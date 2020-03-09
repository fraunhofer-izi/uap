import sys
from abstract_step import *
import glob
import misc
import process_pool
import yaml
import os

from logging import getLogger

logger=getLogger('uap_logger')

class StringTiePrepDE(AbstractStep):

    '''StringTie is a fast and highly efficient assembler of RNA-Seq alignments into potential
    transcripts. prepDE.py is Python script to extract this read count information directly from the
    files generated by StringTie (run with the -e parameter). It generates two CSV files containing
    the count matrices for genes and transcripts, using the coverage values found in the output of
    stringtie -e

    Here, all the transcripts.gtf files from a previous stringtie call are collected, written to a
    file and provided to the prepDE.py script for conversion (all at once).

    NOTE: This step implements the prepDE.py part of stringtie. If you want stringtie to assemble
    transcripts from multiple BAM files please or merge assemblies use step stringtie or
    stringtie_merge, resp.!

    https://ccb.jhu.edu/software/stringtie/

    '''
    def __init__(self, pipeline):
        super(StringTiePrepDE, self).__init__(pipeline)

        self.set_cores(6)

        # The transcripts that should be merged
        self.add_connection('in/features')

        self.add_connection('out/gene_matrix')
        self.add_connection('out/transcript_matrix')
        self.add_connection('out/legend')
        self.add_connection('out/log_stderr')
        self.add_connection('out/log_stdout')

        self.require_tool('prepDE')
        self.require_tool('printf')

        ## options for stringtie program
        # -l LENGTH, --length=LENGTH
        self.add_option('length', int, optional = True,
                        description = 'the average read length [default: 75]')
        # -p PATTERN, --pattern=PATTERN
        self.add_option('pattern', str, optional = True,
                        description = 'a regular expression that selects the sample subdirectories')
        # -c, --cluster
        self.add_option('cluster', bool, optional = True,
                        description = 'whether to cluster genes that overlap with different '
                        'gene IDs, ignoring ones with geneID pattern (see below)')
        # -s STRING, --string=STRING
        self.add_option('string', str, optional = True,
                        description = 'if a different prefix is used for geneIDs assigned by '
                        'StringTie [default: MSTRG')
        # -k KEY, --key=KEY
        self.add_option('key', str, optional = True,
                        description = 'if clustering, what prefix to use for geneIDs assigned '
                        'by this script [default: prepG]')
        # --legend=LEGEND writes a file if clustering is enabled, assessed via uap

        self.add_option('run_id', str, optional=True, default="prepDEall",
                        description="A name for the run. Since this step merges multiple samples "
                        "into a single one, the run_id cannot be the sample name anymore.")

    def runs(self, run_ids_connections_files):

        # Compile the list of options
        options=['length','pattern','cluster','string','key']

        set_options = [option for option in options if \
                       self.is_option_set_in_config(option)]

        option_list = list()
        for option in set_options:
            if isinstance(self.get_option(option), bool):
                if self.get_option(option):
                    option_list.append('--%s' % option)
            else:
                option_list.append( '--%s' % option )
                option_list.append( str(self.get_option(option)) )

        run_id = self.get_option('run_id')

        # Get all input abundances txt files that should be merged
        input_list = list()

        for sample_id in run_ids_connections_files.keys():

            input_paths = run_ids_connections_files[sample_id]["in/features"]
            input_list.append('%s\t%s' % (sample_id, input_paths[0]))

        with self.declare_run(run_id) as run:

            lstfile = run.add_temporary_file('input_list',
                                             designation = 'input')

            genematfile = run.add_output_file('gene_matrix',
                                              '%s-prepDE_gene_count_matrix.csv' % run_id,
                                              input_paths)
            transmatfile = run.add_output_file('transcript_matrix',
                                               '%s-prepDE_transcript_count_matrix.csv' % run_id,
                                               input_paths)
            legendfile = run.add_output_file('legend',
                                               '%s-prepDE_legend.csv' % run_id,
                                               input_paths)
            stdout = run.add_output_file('log_stdout',
                                         '%s-prepDE_counts.stdout' % run_id,
                                         input_paths)
            stderr = run.add_output_file('log_stderr',
                                         '%s-prepDE_counts.stderr' % run_id,
                                         input_paths)

            with run.new_exec_group() as create_list_group:

                print_list = [self.get_tool('printf'),
                              '\n'.join(input_list)]

                create_list_group.add_command(print_list,
                                              stdout_path = lstfile)

            with run.new_exec_group() as prepDE_group:

                stringtie_prep = [self.get_tool('prepDE'),
                                   '-i', lstfile]

                stringtie_prep.extend(option_list)

                stringtie_prep.extend(['-g', genematfile])
                stringtie_prep.extend(['-t', transmatfile])
                stringtie_prep.extend(['--legend=%s' % legendfile])


                prepDE_group.add_command(stringtie_prep,
                                         stdout_path = stdout,
                                         stderr_path = stderr)
