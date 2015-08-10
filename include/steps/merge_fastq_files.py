from abstract_step import *


class MergeFastqFiles(AbstractStep):
    '''
    Merge all .fastq(.gz) files of a sample.
    '''
    
    def __init__(self, pipeline):
        super(MergeFastqFiles, self).__init__(pipeline)
        
        self.set_cores(1) # muss auch in den Decorator
        
        self.add_connection('in/first_read')
        self.add_connection('in/second_read')
        self.add_connection('out/first_read')
        self.add_connection('out/second_read')
        
        self.require_tool('cat')
        self.require_tool('dd')
        self.require_tool('mkfifo')
        self.require_tool('pigz')

    def runs(self, run_ids_connections_files):
        '''
        self.runs() should be a replacement for declare_runs() and execute_runs()
        All information given here should end up in the step object which is 
        provided to this method.
        '''
        read_types = {'first_read': '_R1', 'second_read': '_R2'}
        for run_id in run_ids_connections_files.keys():
            with self.declare_run(run_id) as run:
                for read in read_types:
                    connection = 'in/%s' % read
                    input_paths = run_ids_connections_files[run_id][connection]

                    if input_paths == [None]:
                        run.add_empty_output_connection("%s" % read)
                    else:
                        temp_fifos = list()
                        exec_group = run.new_exec_group()
                        for input_path in input_paths:
                            # Gzipped files are unpacked first
                            # !!! Might be worth a try to use fifos instead of
                            #     temp files!!!
                            # 1. Create temporary fifo
                            temp_fifo = run.add_temporary_file(
                                "fifo-%s" %
                                os.path.basename(input_path) )
                            temp_fifos.append(temp_fifo)
                            mkfifo = [self.get_tool('mkfifo'), temp_fifo]
                            exec_group.add_command(mkfifo)

                            # 2. Output files to fifo
                            if input_path.endswith('fastq.gz'):
                                with exec_group.add_pipeline() as unzip_pipe:
                                    # 2.1 command: Read file in 4MB chunks
                                    dd_in = [self.get_tool('dd'),
                                           'ibs=4M',
                                           'if=%s' % input_path]
                                    # 2.2 command: Uncompress file to fifo
                                    pigz = [self.get_tool('pigz'),
                                            '--decompress',
                                            '--stdout']
                                    # 2.3 Write file in 4MB chunks to fifo
                                    dd_out = [self.get_tool('dd'),
                                              'obs=4M',
                                              'of=%s' % temp_fifo]
                                
                                    unzip_pipe.add_command(dd_in)
                                    unzip_pipe.add_command(pigz)
                                    unzip_pipe.add_command(dd_out)
                            elif input_path.endswith('fastq'):
                                # 2.1 command: Read file in 4MB chunks and
                                #              write to fifo in 4MB chunks
                                dd_in = [self.get_tool('dd'),
                                         'bs=4M',
                                         'if=%s' % input_path,
                                         'of=%s' % temp_fifo]
                                exec_group.add_command(dd_in)
                            else:
                                raise StandardError("File %s does not end with "
                                                    "any expected suffix ("
                                                    "fastq.gz or fastq). Please "
                                                    "fix that issue.")
                        # 3. Read data from fifos
                        with exec_group.add_pipeline() as pigz_pipe:
                            # 3.1 command: Read from ALL fifos
                            cat = [self.get_tool('cat')]
                            cat.extend(temp_fifos)
                            # 3.2 Gzip output file
                            pigz = [self.get_tool('pigz'),
                                    '--stdout']
                            # 3.3 command: Write to output file in 4MB chunks
                            stdout_path = run.add_output_file(
                                "%s" % read,
                                "%s%s.fastq.gz" %
                                (run_id, read_types[read]),
                                input_paths)
                            dd = [self.get_tool('dd'),
                                  'obs=4M',
                                  'of=%s' % stdout_path]
                            
                            pigz_pipe.add_command(cat)
                            pigz_pipe.add_command(pigz)
                            pigz_pipe.add_command(dd)
