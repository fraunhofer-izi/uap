import base64
import copy
import csv
import datetime
import fscache
import glob
import json
import logging
from operator import itemgetter
import os
import re
import StringIO
import subprocess
import sys
import yaml

import abstract_step
import misc
import task as task_module
from xml.dom import minidom


logger = logging.getLogger("uap_logger")

# an exception class for reporting configuration errors
class ConfigurationException(Exception):
    def __init__(self, value):
        self.value = value
    def __str__(self):
        return repr(self.value)

class Pipeline(object):
    
    '''
    The Pipeline class represents the entire processing pipeline which is defined
    and configured via the configuration file config.yaml.
    
    Individual steps may be defined in a tree, and their combination with samples
    as generated by one or more source leads to an array of tasks.
    '''

    states = misc.Enum(['WAITING', 'READY', 'QUEUED', 'EXECUTING', 'FINISHED'])
    '''
    Possible states a task can be in.
    '''

    pipeline_path = os.path.dirname(os.path.realpath(__file__))
    '''
    Absolute path to this very file. It is used to circumvent path issues.
    '''

    cluster_config = {
        'slurm':
           {'submit': 'sbatch',
            'stat': 'squeue',
            'template': pipeline_path + '/../submit-scripts/sbatch-template.sh',
            'hold_jid': '--dependency=afterany:%s',
            'set_job_name': '--job-name=%s',
            'set_stderr': '-e',
            'set_stdout': '-o',
            'parse_job_id': 'Submitted batch job (\d+)'},

        'sge':
           {'submit': 'qsub',
            'stat': 'qstat',
            'template': pipeline_path + '/../submit-scripts/qsub-template.sh',
            'hold_jid': '-hold_jid',
            'set_job_name': '-N',
            'set_stderr': '-e',
            'set_stdout': '-o',
            'parse_job_id': 'Your job (\d+)'},

        'uge':
           {'submit': 'qsub',
            'stat': 'qstat',
            'template': pipeline_path + '/../submit-scripts/qsub-template.sh',
            'hold_jid': '-hold_jid',
            'set_job_name': '-N',
            'set_stderr': '-e',
            'set_stdout': '-o',
            'parse_job_id': 'Your job (\d+)'}
}
    '''
    Cluster-related configuration for every cluster system supported.
    '''

    def __init__(self, **kwargs):
        self.caught_signal = None
        
        self.git_dirty_diff = None
        
        self.cluster_type = None
        '''
        The cluster type to be used (must be one of the keys specified in
        cluster_config).
        '''

        # now determine the Git hash of the repository
        command = ['git', 'describe', '--all', '--dirty', '--long']
        try:
            self.git_hash_tag = subprocess.check_output(command).strip()
        except:
            raise StandardError("Execution of %s failed." % " ".join(command))

        # check if we got passed an 'arguments' parameter
        # this parameter should contain a argparse.Namespace object
        args = None
        if 'arguments' in kwargs:
            args = kwargs['arguments']
      
        if '-dirty' in self.git_hash_tag:
            if not args.even_if_dirty:
                print("The repository has uncommitted changes, which is why " +
                      "we will exit right now.")
                print("If this is not a production environment, you can skip " +
                      "this test by specifying --even-if-dirty on the command " +
                      "line.")
                exit(1)
                command = ['git', 'diff']
                try:
                    self.git_dirty_diff = subprocess.check_output(command)
                except:
                    raise StandardError("Execution of %s failed." % 
                                        " ".join(command))
        try:
            # set cluster type
            if args.cluster == 'auto':
                self.set_cluster_type(self.autodetect_cluster_type())
            else:
                self.set_cluster_type(args.cluster)
        except AttributeError:
            # cluster type is not an applicable parameter here, and that's fine
            # (we're probably in run-locally.py)
            pass

        # the configuration as read from config.yaml
        self.config = dict()

        # dict of steps, steps are objects with inter-dependencies
        self.steps = dict()
        
        # topological order of step names
        self.topological_step_order = list()
        
        self.file_dependencies = dict()
        '''
        This dict stores file dependencies within this pipeline, but regardless
        of step, output file tag or run ID. This dict has, for all output 
        files generated by the pipeline, a set of input files that output 
        file depends on.
        '''
        
        self.file_dependencies_reverse = dict()
        '''
        This dict stores file dependencies within this pipeline, but regardless
        of step, output file tag or run ID. This dict has, for all input
        files required pipeline, a set of output files which are generated
        using this input file.
        '''
        
        self.task_id_for_output_file = dict()
        '''
        This dict stores a task ID for every output file created by the pipeline.
        '''

        self.task_ids_for_input_file = dict()
        '''
        This dict stores a set of task IDs for every input file used in the
        pipeline.
        '''

        self.input_files_for_task_id = dict()
        '''
        This dict stores a set of input files for every task id in the pipeline.
        '''

        self.output_files_for_task_id = dict()
        '''
        This dict stores a set of output files for every task id in the pipeline.
        '''

        self.config_file_name = args.config.name
        '''
        This stores the name of the configuration file of the current analysis
        '''

        self.read_config(args.config)

        # collect all tasks
        self.task_for_task_id = {}
        self.all_tasks_topologically_sorted = []
        for step_name in self.topological_step_order:
            step = self.steps[step_name]
            logger.debug("Collect now all tasks for step: %s" % step)
            for run_index, run_id in enumerate(misc.natsorted(step.get_run_ids())):
                task = task_module.Task(self, step, run_id, run_index)
                # if any run of a step contains an exec_groups,
                # the task (step/run) is added to the task list
                run = step.get_run(run_id)
                logger.debug("Step: %s, Run: %s" % (step, run_id))
                run_has_exec_groups = False
                if len(run.get_exec_groups()) > 0:
                    run_has_exec_groups = True
                if run_has_exec_groups:
                    logger.debug("Task: %s" % task)
                    self.all_tasks_topologically_sorted.append(task)
                # Fail if multiple tasks with the same name exist
                if str(task) in self.task_for_task_id:
                    raise ConfigurationException("Duplicate task ID %s." % str(task))
                self.task_for_task_id[str(task)] = task

        self.tool_versions = {}
        self.check_tools()

    # read configuration and make sure it's good
    def read_config(self, config_file):
        #print >> sys.stderr, "Reading configuration..."
#        self.config = yaml.load(open('config.yaml'))
        self.config = yaml.load(config_file)

        if not 'id' in self.config:
            self.config['id'] = config_file.name
        
        
        if not 'destination_path' in self.config:
            raise ConfigurationException("Missing key: destination_path")
        if not os.path.exists(self.config['destination_path']):
            raise ConfigurationException("Destination path does not exist: " 
                                         + self.config['destination_path'])

        if not os.path.exists("%s-out" % self.config['id']):
            os.symlink(self.config['destination_path'], '%s-out' % self.config['id'])

        self.build_steps()
        
    def build_steps(self):
        self.steps = {}
        if not 'steps' in self.config:
            raise ConfigurationException("Missing key: steps")
        
        re_simple_key = re.compile('^[a-zA-Z0-9_]+$')
        re_complex_key = re.compile('^([a-zA-Z0-9_]+)\s+\(([a-zA-Z0-9_]+)\)$')

        # step one: instantiate all steps
        for step_key, step_description in self.config['steps'].items():
            
            # the step keys in the configuration may be either:
            # - MODULE_NAME 
            # - DIFFERENT_STEP_NAME\s+\(MODULE_NAME\)
            step_name = None
            module_name = None
            if re_simple_key.match(step_key):
                step_name = step_key
                module_name = step_key
            else:
                match = re_complex_key.match(step_key)
                if match:
                    step_name = match.group(1)
                    module_name = match.group(2)
            
            if step_name == 'temp':
                # A step cannot be named 'temp' because we need the out/temp
                # directory to store temporary files.
                raise ConfigurationException("A step name cannot be 'temp'.")
            
            step_class = abstract_step.AbstractStep.get_step_class_for_key(module_name)
            step = step_class(self)
            
            step.set_step_name(step_name)
            step.set_options(step_description)
            
            self.steps[step_name] = step
            
        # step two: set dependencies
        for step_name, step in self.steps.items():
            if not step.needs_parents:
                if '_depends' in step._options:
                    raise ConfigurationException("%s must not have dependencies "
                        "because it declares no in/* connections (remove the "
                        "_depends key)." % step_name)
            else:
                if not '_depends' in step._options:
                    raise ConfigurationException("Missing key in step '%s': "
                        "_depends (set to null if the step has no dependencies)." 
                        % step_name)
                depends = step._options['_depends']
                if depends == None:
                    pass
                else:
                    temp_list = depends
                    if depends.__class__ == str:
                        temp_list = [depends]
                    for d in temp_list:
                        if not d in self.steps:
                            raise ConfigurationException("Step %s specifies "
                                "an undefined dependency: %s." % (step_name, d))
                        step.add_dependency(self.steps[d])
                        
        # step three: perform topological sort, raise a ConfigurationException
        # if there's a cycle (yeah, the algorithm is O(n^2), tsk, tsk...)
        
        unassigned_steps = set(self.steps.keys())
        assigned_steps = set()
        self.topological_step_order = []
        while len(unassigned_steps) > 0:
            # choose all tasks which have all dependencies resolved, either
            # because they have no dependencies or are already assigned
            next_steps = []
            for step_name in unassigned_steps:
                is_ready = True
                for dep in self.steps[step_name].dependencies:
                    dep_name = dep.get_step_name()
                    if not dep_name in assigned_steps:
                        is_ready = False
                        break
                if is_ready:
                    next_steps.append(step_name)
            if len(next_steps) == 0:
                raise ConfigurationException(
                    "There is a cycle in the step dependencies.")
            for step_name in misc.natsorted(next_steps):
                self.topological_step_order.append(step_name)
                assigned_steps.add(step_name)
                unassigned_steps.remove(step_name)
                
        # step four: finalize step
        for step in self.steps.values():
            step.finalize()

    def print_source_runs(self):
        for step_name in self.topological_step_order:
            step = self.steps[step_name]
            if isinstance(step, abstract_step.AbstractSourceStep):
                for run_id in misc.natsorted(step.get_run_ids()):
                    print("%s/%s" % (step, run_id))

    def add_file_dependencies(self, output_path, input_paths):
        if output_path in self.file_dependencies:
            raise StandardError("Different steps/runs/tags want to create "
                                "the same output file: %s." % output_path)
        self.file_dependencies[output_path] = set(input_paths)
        
        for inpath in input_paths:
            if not inpath in self.file_dependencies_reverse:
                self.file_dependencies_reverse[inpath] = set()
            self.file_dependencies_reverse[inpath].add(output_path)
        
    def add_task_for_output_file(self, output_path, task_id):
        if output_path in self.task_id_for_output_file:
            raise StandardError("More than one step is trying to create the "
                "same output file: %s." % output_path)
        self.task_id_for_output_file[output_path] = task_id
        
        if not task_id in self.output_files_for_task_id:
            self.output_files_for_task_id[task_id] = set()
        self.output_files_for_task_id[task_id].add(output_path)

    def add_task_for_input_file(self, input_path, task_id):
        if not input_path in self.task_ids_for_input_file:
            self.task_ids_for_input_file[input_path] = set()
        self.task_ids_for_input_file[input_path].add(task_id)
        
        if not task_id in self.input_files_for_task_id:
            self.input_files_for_task_id[task_id] = set()
        self.input_files_for_task_id[task_id].add(input_path)

    def check_command(self, command):
        for argument in command:
            if not isinstance(argument, str):
                raise StandardError(
                    "The command to be launched '%s' " % command +
                    "contains non-string argument '%s'. " % argument + 
                    "Therefore the command will fail. Please " +
                    "fix this type issue.")
        return

    def exec_pre_post_calls(self, tool_id, info_key, info_command, 
                            tool_check_info):
        if info_command.__class__ == str:
            info_command = [info_command]
        for command in info_command:
            if type(command) is str:
                command = command.split()
            self.check_command(command)
            try:
                proc = subprocess.Popen(
                    command,
                    stdin = None,
                    stdout = subprocess.PIPE,
                    stderr = subprocess.PIPE,
                    close_fds = True)
                
            except OSError as e:
                raise ConfigurationException(
                    "Error while executing '%s' for %s: %s "
                    "Error no.: %s Error message: %s" % 
                    (info_key, tool_id, " ".join(command), e.errno, e.strerror))

            command_call = info_key
            command_exit_code = '%s-exit-code' % info_key
            command_response = '%s-respone' % info_key        
            (output, error) = proc.communicate()
            if info_key in ['module_load', 'module_unload']:
                exec output
                tool_check_info.update({
                    command_call : (' '.join(command)).strip(),
                    command_exit_code : proc.returncode
                })
                sys.stderr.write(error)
                sys.stderr.flush()
            else:
                tool_check_info.update({
                    command_call : (' '.join(command)).strip(),
                    command_exit_code : proc.returncode,
                    command_response : (output + error)
                })

        return tool_check_info

    def check_tools(self):
        '''
        checks whether all tools references by the configuration are available 
        and records their versions as determined by ``[tool] --version`` etc.
        '''
        if not 'tools' in self.config:
            return
        for tool_id, info in self.config['tools'].items():
            tool_check_info = dict()

            # Load module(s) and execute command if configured
            for pre_cmd in (x for x in ('module_load', 'pre_command') 
                             if x in info):
                tool_check_info = self.exec_pre_post_calls(
                    tool_id, pre_cmd, info[pre_cmd], tool_check_info)
                
            # Execute command to check if tool is available
            command = [copy.deepcopy(info['path'])]
            if info['path'].__class__ == list:
                command = copy.deepcopy(info['path'])
            self.check_command(command)
            if 'get_version' in info:
                command.append(info['get_version'])
            try:
                proc = subprocess.Popen(
                    command,
                    stdin = subprocess.PIPE,
                    stdout = subprocess.PIPE,
                    stderr = subprocess.PIPE, 
                    close_fds = True)
                proc.stdin.close()
            except OSError as e:
                raise ConfigurationException("Error while checking Tool %s " 
                                             "Error no.: %s Error message: %s" %
                                             (info['path'], e.errno, e.strerror))
            proc.wait()
            exit_code = None
            exit_code = proc.returncode
            tool_check_info.update({
                'command': (' '.join(command)).strip(),
                'exit_code': exit_code,
                'response': (proc.stdout.read() + proc.stderr.read()).strip()
            })
            # print("Command: %s" % tool_check_info['command'])
            # print("Exit Code: %s" % tool_check_info['exit_code'])
            # print("Response: %s" % tool_check_info['response'])
            expected_exit_code = 0
            if 'exit_code' in info:
                expected_exit_code = info['exit_code']
            if exit_code != expected_exit_code:
                raise ConfigurationException(
                    "Tool check failed for %s: %s - exit code is: %d (expected "
                    "%d)" % (tool_id, ' '.join(command), exit_code, 
                             expected_exit_code))

            # Execute clean-up command (if configured)
            for info_key in (x for x in ('module_unload', 'post_command') 
                             if x in info):
                tool_check_info = self.exec_pre_post_calls(
                    tool_id, info_key, info[info_key], tool_check_info)
            # Store captured information
            self.tool_versions[tool_id] = tool_check_info

    def notify(self, message, attachment = None):
        '''
        prints a notification to the screen and optionally delivers the
        message on additional channels (as defined by the configuration)
        '''
        print(message.split("\n")[0])
        if 'notify' in self.config:
            try:
                notify = self.config['notify']
                match = re.search('^(http://[a-z\.]+:\d+)/([a-z0-9]+)$', notify)
                if match:
                    host = match.group(1)
                    token = match.group(2)
                    args = ['curl', host, '-X', 'POST', '-d', '@-']
                    proc = subprocess.Popen(args, stdin = subprocess.PIPE)
                    data = {'token': token, 'message': message}
                    if attachment:
                        data['attachment_name'] = attachment['name']
                        data['attachment_data'] = base64.b64encode(attachment['data'])
                    proc.stdin.write(json.dumps(data))
                    proc.stdin.close()
                    proc.wait()
            except:
                # swallow all exception that happen here, failing notifications
                # are no reason to crash the entire thing
                pass

    def check_ping_files(self, print_more_warnings = False, print_details = False, fix_problems = False):
        run_problems = list()
        queue_problems = list()
        check_queue = True
        
        try:
            stat_output = subprocess.check_output([self.cc('stat')], 
                                                  stderr = subprocess.STDOUT)
        except KeyError:
            check_queue = False
        except OSError:
            check_queue = False
        except subprocess.CalledProcessError:
            # we don't have a stat tool here, don't check the queue
            check_queue = False
            
        if print_more_warnings and not check_queue:
            print("Attention, we cannot check stale queued ping files because "
                  "this host does not have %s." % self.cc('stat'))
            
        running_jids = set()
        
        if check_queue:
            for line in stat_output.split("\n"):
                try:
                    jid = int(line.strip().split(' ')[0])
                    running_jids.add(str(jid))
                except ValueError:
                    # this is not a JID
                    pass
        
        now = datetime.datetime.now()
        for which in ['run', 'queued']:
            if not check_queue and which == 'queued':
                continue
            for task in self.all_tasks_topologically_sorted:
                path = task.step._get_ping_path_for_run_id(task.run_id, which)
                if os.path.exists(path):
                    if which == 'run':
                        info = yaml.load(open(path, 'r'))
                        start_time = info['start_time']
                        last_activity = datetime.datetime.fromtimestamp(
                            abstract_step.AbstractStep.fsc.getmtime(path))
                        last_activity_difference = now - last_activity
                        if last_activity_difference.total_seconds() > \
                           abstract_step.AbstractStep.PING_TIMEOUT:
                            run_problems.append((task, path, last_activity_difference, last_activity - start_time))
                    if which == 'queued':
                        info = yaml.load(open(path, 'r'))
                        if not str(info['job_id']) in running_jids:
                            queue_problems.append((task, path, info['submit_time']))
           
        show_hint = False
        
        if len(run_problems) > 0:
            show_hint = True
            label = "Warning: There are %d stale run ping files." % len(run_problems)
            print(label)
            if print_details:
                print('-' * len(label))
                run_problems = sorted(run_problems, key=itemgetter(2, 3), reverse=True)
                for problem in run_problems:
                    task = problem[0]
                    path = problem[1]
                    last_activity_difference = problem[2]
                    ran_for = problem[3]
                    print("dead since %13s, ran for %13s: %s" % (
                        misc.duration_to_str(last_activity_difference), 
                        misc.duration_to_str(ran_for), task))
                print("")
                    
        if len(queue_problems) > 0:
            show_hint = True
            label = "Warning: There are %d tasks marked as queued, but they do not seem to be queued." % len(queue_problems)
            print(label)
            if print_details:
                print('-' * len(label))
                queue_problems = sorted(queue_problems, key=itemgetter(2), reverse=True)
                for problem in queue_problems:
                    task = problem[0]
                    path = problem[1]
                    start_time = problem[2]
                    print("submitted at %13s: %s" % (start_time, task))
                print("")
                
        if fix_problems:
            all_problems = run_problems
            all_problems.extend(queue_problems)
            for problem in all_problems:
                path = problem[1]
                print("Now deleting %s..." % path)
                os.unlink(path)
                
        if show_hint:
            if print_more_warnings and not print_details or not fix_problems:
                print("Hint: Run 'uap %s fix-problems --details' to see the "
                      "details."  % self.config_file_name)
            if not fix_problems:
                print("Hint: Run 'uap %s fix-problems --srsly' to fix these "
                      "problems (that is, delete all problematic ping files)."
                      % self.config_file_name)

    def check_volatile_files(self, details = False, srsly = False):
        collected_files = set()
        for task in self.all_tasks_topologically_sorted:
            collected_files |= task.volatilize_if_possible(srsly)
        if not srsly and len(collected_files) > 0:
            if details:
                for path in sorted(collected_files):
                    print(path)
            total_size = 0
            for path in collected_files:
                total_size += os.path.getsize(path)
            print("Hint: You could save %s of disk space by volatilizing %d "
                  "output files." % (misc.bytes_to_str(total_size),
                                     len(collected_files)))
            print("Call ./volatilize.py --srsly to purge the files.")

    def autodetect_cluster_type(self):
        try:
            if ( subprocess.check_output( ["sbatch", "--version"])[:6] == "slurm "):
                return "slurm"
        except OSError:
            pass

        try:
            if ( subprocess.check_output(["qstat", "-help"] )[:4] == "SGE "):
                return "sge"
        except OSError:
            pass

        try:
            if ( subprocess.check_output(["qstat", "-help"] )[:4] == "UGE "):
                return "uge"
        except OSError:
            pass

        return None

    def set_cluster_type(self, cluster_type):
        if not cluster_type in Pipeline.cluster_config:
            print("Unknown cluster type: %s (choose one of %s)." % (
                cluster_type, ', '.join(Pipeline.self.cluster_config.keys())))
            exit(1)
        self.cluster_type = cluster_type

    '''
    Shorthand to retrieve a cluster-type-dependent command or filename (cc == cluster command).
    '''
    def cc(self, key):
        return Pipeline.cluster_config[self.cluster_type][key]

    '''
    Shorthand to retrieve a cluster-type-dependent command line part (this is a list)
    '''
    def ccla(self, key, value):
        result = Pipeline.cluster_config[self.cluster_type][key]
        if '%s' in result:
            return [result % value]
        else:
            return [result, value]
