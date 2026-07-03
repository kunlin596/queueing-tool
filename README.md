# queueing-tool

A tool for scheduling multiple parallelized jobs to a machine. Jobs are scheduled
according to a priority queue. The priority of a job is small if the amount of
requested resources/computation time is large and high if the amount of requested
resources/computation time is small, respectively. Priority of a job increases with
its waiting time.

## Installation

The tool is a Python package (stdlib-only). The recommended install is
[uv](https://docs.astral.sh/uv/):

```bash
uv tool install queueing-tool          # from PyPI (when published)
uv tool install -e ~/dev/src/queueing-tool  # or editable from a checkout
```

This puts `qsub`, `qstat`, `qdel`, `qinfo`, and `qserver` on your `PATH`
(`~/.local/bin`). `pipx install` works the same way. The legacy `queue/`
scripts remain as shims that run the package straight from the checkout —
no install needed for daemon hosts that already point `PATH` at `queue/`.

To run the scheduler daemon: open the file `qserver_daemon` and set the
available resources `DEVICE_IDS`, `THREADS`, and `MEMORY` to values that fit
for your machine. Then install it via `install.sh` (root) or manually below.

### Automatic

Run the script `install.sh` as root.

### Manual

Copy the `queue` directory to a location of your choice. The queueing tool requires
to run the `qserver` tool that manages all submitted jobs and allocates the
requested resources. The server can either be started manually or automatically
as a daemon. For the latter case, copy the `qserver_daemon` script to `/etc/init.d/`
and invoke

```bash
sudo update-rc.d qserver_daemon defaults
```

to register it for startup during boot. Note that the path to the queue directory
and the available resources need to be specified in `qserver_daemon` at the point
marked with `TODO`. You can now also start/stop the qserver with

```bash
/etc/init.d/qserver_daemon start/stop
```

The queue-commands are located the `queue` directory. In order to submit jobs, it
is required that the following line is added either to your `.bashrc` (will make the
queue tools accessible for your account) or to an empty file in `/etc/profiles.d/`
(will make the queue tools accessible for all accounts):

```bash
export PATH=/your/queue/path/queue:$PATH
```

## Quickstart

Save the following as `test.sh`:

```bash
# Simple script that counts up numbers forever:
#block(name=test-job, threads=1, memory=1000, hours=24)
  counter=0
  while true; do
    echo "Job running: counter=$counter"
    counter=$((counter + 1))
    sleep 1
  done
```

Submit the script:

```bash
qsub test.sh
```

Check the job status in the queue with `qstat`. Once the job starts, output logs are
available in the folder `q.log/test-job.<JOB_ID>`. Stop the job with `qdel <JOB_ID>`.

## Usage

### qserver

Either start the server automatically as described above or manually:

```bash
qserver [--port PORT] [--gpus GPUS] [--threads THREADS] [--memory MEMORY] [--abort_on_time_limit]
```

| Option | Description |
| --- | --- |
| `--port PORT` | port to listen on (default: 1234) |
| `--gpus GPUS` | comma separated list of available gpu device ids |
| `--threads THREADS` | number of available threads/cores |
| `--memory MEMORY` | available main memory in mb |
| `--abort_on_time_limit` | kill jobs if time limit is exceeded |

### Queue scripts

The queue uses bash scripts that are organized in blocks. A block is a part
of the script that forms an independent job and can be specified as follows:

```bash
#block(name=[jobname], threads=[num-threads], memory=[max-memory], subtasks=[num-subtasks], gpus=[num-gpus], hours=[time-limit])
```

| Parameter | Description | Default |
| --- | --- | --- |
| `name` | the name of the job | `unknown-job` |
| `threads` | the number of threads to use | `1` |
| `memory` | the maximal amount of memory in mb for the job | `1024` |
| `subtasks` | the number of subtasks in the block, see below | `1` |
| `gpus` | the number of requested GPUs | `0` |
| `hours` | the maximal runtime of the job in hours | `1` |

Each block is scheduled `[subtasks]` times in parallel, which for instance allows
for easy data parallelism. The subtask of the actual job is specified with the
`$SUBTASK_ID` variable and the total number of subtasks is contained in
`$N_SUBTASKS`. Subtask IDs range from 1 to `$N_SUBTASKS`.

If more than one block is specified in one script, each block is considered to
be dependent on its predecessor, i.e. it is not started before all subtasks of
the preceding block have finished. Subtasks within a block can run in parallel.

Example:

```bash
#block(name=block-1, threads=2, memory=2000, subtasks=10, hours=24)
  echo "process subtask $SUBTASK_ID of $N_SUBTASKS"
  ./processData data-dir/data.part-$SUBTASK_ID

#block(name=block-2, threads=10, memory=10000, hours=2)
  ./doSomethingThatRequiresTheResultsFromBlock-1
  ./somethingElse
  echo "processed block-2 after all subtasks of block-1"
```

The first block has 10 subtasks that all process different data, if possible in
parallel. The second block is not started before all subtasks of the first block are
finished. Note that all block parameters that are not specified are set
to the default values.

In order to submit the above script, save it as `your-queue-script.sh` and invoke

```bash
qsub your-queue-script.sh [param1] [param2] [...]
```

Note that the tool will create a `q.log` folder in the directory you invoked `qsub` in.
In this folder, a file for each job is created, storing some meta information about the
job as well as everything written to stdout and stderr.

### Job status

| Status | Description |
| --- | --- |
| `r` (running) | The requested resources have been allocated and the job runs. |
| `w` (waiting) | The requested resources could not yet be allocated and the job waits for execution. |
| `h` (hold) | The job has to wait for other jobs to be finished before it can start. |

### Commands

#### qsub

Submit a job to the queue.

```bash
qsub [options...] script [script params]
```

| Option | Description |
| --- | --- |
| `script` | Script (plus its parameters) to be submitted. Script parameters must not start with `-`. If they do so, pass them with escaped quotation marks and an escaped leading space like this: `\"\ --foo\"`. |
| `-l, --local` | Execute the script locally |
| `-b BLOCK, --block BLOCK` | Only submit/execute the specified block |
| `-s BLOCK SUBTASK_ID, --subtask BLOCK SUBTASK_ID` | Arguments for this option are a block name and a subtask id. Only submit/execute the subtask id of the specified block. |
| `-f FROM_BLOCK, --from_block FROM_BLOCK` | Submit/execute the specified block and all succeeding blocks |
| `--server_ip SERVER_IP` | Ip address of the server (default: localhost) |
| `--server_port SERVER_PORT` | Port of the server (default: 1234) |

#### qstat

```bash
qstat [-v]
```

Prints all jobs that are currently submitted. If option `-v` is set, output is verbose, i.e. requested resources per job are also displayed.

#### qtop

```bash
qtop [--dump]
```

Interactive qstat TUI with per-job log viewing. The list view shows the
auto-refreshed job table (arrows/`j`/`k` to highlight, Enter or mouse click
to select, `f` to also list finished logs, `q` or Ctrl+C to quit). Selecting
a job opens its log in an external viewer chain and returns to the list on
`q`:

1. [`tailspin`](https://github.com/bensadeh/tailspin) (`tspin`) — zero-config
   log highlighting on top of less; follows running jobs live.
2. `less -R` — `+F` live follow for running jobs (Ctrl+C pauses into
   scroll/search mode, `F` re-follows), `+G` opens finished logs at the end.
3. A builtin tail view when neither tool is installed.

Log paths are tracked: every job records its absolute `q.log` path in
`~/.local/state/queueing-tool/job_logs/<id>` at submission, so qtop works
from any directory. `--dump` prints the parsed rows and resolved log paths
for scripting.

**Optional viewer dependencies** (recommended): `less` (usually present) and
`tailspin` —

```bash
# static binary, no root needed:
curl -sL https://github.com/bensadeh/tailspin/releases/latest/download/tailspin-x86_64-unknown-linux-musl.tar.gz \
  | tar xz -C ~/.local/bin tspin
# or: cargo install tailspin / brew install tailspin
```

#### qdel

Delete jobs.

```bash
qdel [options...] job_specifier(s)
```

| Option | Description |
| --- | --- |
| `jobs` | Jobs to be deleted. Is a space separated list of job names, user names, or job ids. For ids (neither `-n` nor `-u` option is set), job ranges separated by a `-` are also possible. |
| `--server_ip SERVER_IP` | ip address of the server (default: localhost) |
| `--server_port SERVER_PORT` | port of the server (default: 1234) |
| `-n` | Delete all jobs of the given names. Asterisks can be used as wildcards. |
| `-u` | Delete all jobs of the given users. Asterisks can be used as wildcards. |

Examples:

```bash
qdel 2-5        # deletes the jobs with id 2,3,4,5
qdel 1,3,5      # deletes the jobs with id 1,3,5
qdel -n job_A   # deletes all jobs with name job_A
qdel -u user_x  # deletes all jobs of user user_x
```

Jobs can only be deleted by their owners or by root.

#### qinfo

Outputs some information about the current queue status.
