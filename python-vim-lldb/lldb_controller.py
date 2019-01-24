
#
# This file defines the layer that talks to lldb
#

import os, re, sys
import lldb
import vim
from vim_ui import UI
import logging as lg
logging = lg.getLogger("vim-lldb")

# =================================================
# Convert some enum value to its string counterpart
# =================================================

# Shamelessly copy/pasted from lldbutil.py in the test suite
def state_type_to_str(enum):
    logging.debug("state_type_to_str")
    """Returns the stateType string given an enum."""
    if enum == lldb.eStateInvalid:
        return "invalid"
    elif enum == lldb.eStateUnloaded:
        return "unloaded"
    elif enum == lldb.eStateConnected:
        return "connected"
    elif enum == lldb.eStateAttaching:
        return "attaching"
    elif enum == lldb.eStateLaunching:
        return "launching"
    elif enum == lldb.eStateStopped:
        return "stopped"
    elif enum == lldb.eStateRunning:
        return "running"
    elif enum == lldb.eStateStepping:
        return "stepping"
    elif enum == lldb.eStateCrashed:
        return "crashed"
    elif enum == lldb.eStateDetached:
        return "detached"
    elif enum == lldb.eStateExited:
        return "exited"
    elif enum == lldb.eStateSuspended:
        return "suspended"
    else:
        raise Exception("Unknown StateType enum")


class StepType:
    INSTRUCTION = 1
    INSTRUCTION_OVER = 2
    INTO = 3
    OVER = 4
    OUT = 5


class LLDBController(object):
    """ Handles Vim and LLDB events such as commands and lldb events. """

    # Timeouts (sec) for waiting on new events. Because vim is not
    # multi-threaded, we are restricted to servicing LLDB events from the main
    # UI thread. Usually, we only process events that are already sitting on the
    # queue. But in some situations (when we are expecting an event as a result
    # of some user interaction) we want to wait for it. The constants below set
    # these wait period in which the Vim UI is "blocked". Lower numbers will
    # make Vim more responsive, but LLDB will be delayed and higher numbers will
    # mean that LLDB events are processed faster, but the Vim UI may appear less
    # responsive at times.
    eventDelayStep = 2
    eventDelayLaunch = 1
    eventDelayContinue = 1

    def __init__(self):
        """ Creates the LLDB SBDebugger object and initializes the UI class. """
        logging.debug("LLDBController.__init__")
        self.target = None
        self.process = None
        self.load_dependent_modules = True

        self.dbg = lldb.SBDebugger.Create()
        self.commandInterpreter = self.dbg.GetCommandInterpreter()
        self.commandInterpreter.HandleCommand(
            "settings set target.load-script-from-symbol-file false",
            lldb.SBCommandReturnObject(),
        )

        self.ui = UI()

    def completeCommand(self, a, l, p):
        """ Returns a list of viable completions for command a with length l and cursor at p  """
        logging.debug("LLDBController.completeCommand")

        assert l[0] == "L"
        # Remove first 'L' character that all commands start with
        l = l[1:]

        # Adjust length as string has 1 less character
        p = int(p) - 1

        result = lldb.SBStringList()
        num = self.commandInterpreter.HandleCompletion(l, p, 1, -1, result)

        if num == -1:
            # FIXME: insert completion character... what's a completion character?
            pass
        elif num == -2:
            # FIXME: replace line with result.GetStringAtIndex(0)
            pass

        if result.GetSize() > 0:
            results = filter(
                None, [result.GetStringAtIndex(x) for x in range(result.GetSize())]
            )
            return results
        else:
            return []

    def doStep(self, stepType):
        """ Perform a step command and block the UI for eventDelayStep seconds
        in order to process events on lldb's event queue.
        FIXME: if the step does not complete in eventDelayStep seconds, we
               relinquish control to the main thread to avoid the appearance of
               a "hang". If this happens, the UI will update whenever; usually
               when the user moves the cursor. This is somewhat annoying.
        """
        logging.debug("LLDBController.doStep")
        if not self.process:
            sys.stderr.write("No process to step")
            return

        t = self.process.GetSelectedThread()
        if stepType == StepType.INSTRUCTION:
            t.StepInstruction(False)
        if stepType == StepType.INSTRUCTION_OVER:
            t.StepInstruction(True)
        elif stepType == StepType.INTO:
            t.StepInto()
        elif stepType == StepType.OVER:
            t.StepOver()
        elif stepType == StepType.OUT:
            t.StepOut()

        self.processPendingEvents(self.eventDelayStep, True)

    def doSelect(self, command, args):
        """ Like doCommand, but suppress output when "select" is the first argument."""
        logging.debug("LLDBController.doSelect")
        a = args.split(" ")
        return self.doCommand(command, args, "select" != a[0], True)

    def doProcess(self, args):
        """ Handle 'process' command. If 'launch' is requested, use doLaunch() instead
        of the command interpreter to start the inferior process.
    """
        logging.debug("LLDBController.doProcess")
        a = args.split(" ")
        if len(args) == 0 or (len(a) > 0 and a[0] != "launch"):
            self.doCommand("process", args)
            # self.ui.update(self.target, "", self)
        else:
            self.doLaunch("-s" not in args, "")

    def doAttachById(self, process_id):
        """ Handle process attach.  """
        logging.debug("LLDBController.doAttachById")
        error = lldb.SBError()

        self.processListener = lldb.SBListener("process_event_listener")
        self.target = self.dbg.CreateTarget("")
        self.process = self.target.AttachToProcessWithID(
            self.processListener, int(process_id), error
        )
        if not error.Success():
            sys.stderr.write("Error during attach: " + str(error))
            return

        self.ui.activate()
        self.pid = self.process.GetProcessID()

        print("Attached to %s (pid=%d)" % (process_id, self.pid))

    def doAttach(self, process_name):
        """ Handle process attach.  """
        logging.debug("LLDBController.doAttach")
        error = lldb.SBError()

        self.processListener = lldb.SBListener("process_event_listener")
        self.target = self.dbg.CreateTarget("")
        self.process = self.target.AttachToProcessWithName(
            self.processListener, process_name, False, error
        )
        if not error.Success():
            sys.stderr.write("Error during attach: " + str(error))
            return

        self.ui.activate()
        self.pid = self.process.GetProcessID()

        print("Attached to %s (pid=%d)" % (process_name, self.pid))

    def doDetach(self):
        logging.debug("LLDBController.doDetach")
        if self.process is not None and self.process.IsValid():
            pid = self.process.GetProcessID()
            state = state_type_to_str(self.process.GetState())
            self.process.Detach()
            self.processPendingEvents(self.eventDelayLaunch)

    def doLaunch(self, stop_at_entry, args):
        """ Handle process launch.  """
        logging.debug("LLDBController.doLaunch")
        error = lldb.SBError()

        fs = self.target.GetExecutable()
        exe = os.path.join(fs.GetDirectory(), fs.GetFilename())
        if self.process is not None and self.process.IsValid():
            pid = self.process.GetProcessID()
            state = state_type_to_str(self.process.GetState())
            self.process.Destroy()

        launchInfo = lldb.SBLaunchInfo(args.split(" "))
        self.process = self.target.Launch(launchInfo, error)
        if not error.Success():
            sys.stderr.write("Error during launch: " + str(error))
            return

        # launch succeeded, store pid and add some event listeners
        self.pid = self.process.GetProcessID()
        self.processListener = lldb.SBListener("process_event_listener")
        self.process.GetBroadcaster().AddListener(
            self.processListener, lldb.SBProcess.eBroadcastBitStateChanged
        )

        print("Launched %s %s (pid=%d)" % (exe, args, self.pid))

        if not stop_at_entry:
            self.doContinue()
        else:
            self.processPendingEvents(self.eventDelayLaunch)

    def doTarget(self, args):
        """ Pass target command to interpreter, except if argument is not one of the valid options, or
        is create, in which case try to create a target with the argument as the executable. For example:
          target list        ==> handled by interpreter
          target create blah ==> custom creation of target 'blah'
          target blah        ==> also creates target blah
        """
        logging.debug("LLDBController.doTarget")
        target_args = [  # "create",
            "delete",
            "list",
            "modules",
            "select",
            "stop-hook",
            "symbols",
            "variable",
        ]

        a = args.split(" ")
        if len(args) == 0 or (len(a) > 0 and a[0] in target_args):
            self.doCommand("target", args)
            return
        elif len(a) > 1 and a[0] == "create":
            exe = a[1]
        elif len(a) == 1 and a[0] not in target_args:
            exe = a[0]

        err = lldb.SBError()
        self.target = self.dbg.CreateTarget(
            exe, None, None, self.load_dependent_modules, err
        )
        if not self.target:
            sys.stderr.write("Error creating target %s. %s" % (str(exe), str(err)))
            return

        self.ui.activate()
        self.ui.update(self.target, "created target %s" % str(exe), self)

    def doContinue(self):
        """ Handle 'contiue' command.
        FIXME: switch to doCommand("continue", ...) to handle -i ignore-count param.
    """
        logging.debug("LLDBController.doContinue")

        if not self.process or not self.process.IsValid():
            sys.stderr.write("No process to continue")
            return

        self.process.Continue()
        self.processPendingEvents(self.eventDelayContinue)

    def doBreakpoint(self, args):
        """ Handle breakpoint command with command interpreter, except if the user calls
        "breakpoint" with no other args, in which case add a breakpoint at the line
        under the cursor.
    """
        logging.debug("LLDBController.doBreakpoint")
        a = args.split(" ")
        if len(args) == 0:
            show_output = False

            # User called us with no args, so toggle the bp under cursor
            cw = vim.current.window
            cb = vim.current.buffer
            name = cb.name
            line = cw.cursor[0]

            # Since the UI is responsbile for placing signs at bp locations, we have to
            # ask it if there already is one or more breakpoints at (file, line)...
            if self.ui.haveBreakpoint(name, line):
                bps = self.ui.getBreakpoints(name, line)
                args = "delete %s" % " ".join([str(b.GetID()) for b in bps])
                self.ui.deleteBreakpoints(name, line)
            else:
                args = "set -f %s -l %d" % (name, line)
        else:
            show_output = True

        self.doCommand("breakpoint", args, show_output)
        return

    def doRefresh(self):
        """ process pending events and update UI on request """
        logging.debug("LLDBController.doRefresh")
        status = self.processPendingEvents()

    def doShow(self, name):
        """ handle :Lshow <name> """
        logging.debug("LLDBController.doShow")
        if not name:
            self.ui.activate()
            return

        if self.ui.showWindow(name):
            self.ui.update(self.target, "", self)

    def doHide(self, name):
        """ handle :Lhide <name> """
        logging.debug("LLDBController.doHide")
        if self.ui.hideWindow(name):
            self.ui.update(self.target, "", self)

    def doExit(self):
        logging.debug("LLDBController.doExit")
        self.dbg.Terminate()
        self.dbg = None

    def getCommandResult(self, command, command_args):
        """ Run cmd in the command interpreter and returns (success, output) """
        logging.debug("LLDBController.doCommandResult")
        result = lldb.SBCommandReturnObject()
        cmd = "%s %s" % (command, command_args)

        self.commandInterpreter.HandleCommand(cmd, result)
        return (
            result.Succeeded(),
            result.GetOutput() if result.Succeeded() else result.GetError(),
        )

    def doCommand(self, command, command_args, print_on_success=True, goto_file=False):
        """ Run cmd in interpreter and print result (success or failure) on the vim status line. """
        logging.debug("LLDBController.doCommand")
        (success, output) = self.getCommandResult(command, command_args)
        if success:
            self.ui.update(self.target, "", self, goto_file)
            if len(output) > 0 and print_on_success:
                print(output)
        else:
            sys.stderr.write(output)

    def getCommandOutput(self, command, command_args=""):
        """ runs cmd in the command interpreter andreturns (status, result) """
        logging.debug("LLDBController.doCommandOutput")
        result = lldb.SBCommandReturnObject()
        cmd = "%s %s" % (command, command_args)
        self.commandInterpreter.HandleCommand(cmd, result)
        return (
            result.Succeeded(),
            result.GetOutput() if result.Succeeded() else result.GetError(),
        )

    def processPendingEvents(self, wait_seconds=0, goto_file=True):
        """ Handle any events that are queued from the inferior.
        Blocks for at most wait_seconds, or if wait_seconds == 0,
        process only events that are already queued.
    """
        logging.debug("LLDBController.processPendingEvents")

        status = None
        num_events_handled = 0

        if self.process is not None:
            event = lldb.SBEvent()
            old_state = self.process.GetState()
            new_state = None
            done = False
            if old_state == lldb.eStateInvalid or old_state == lldb.eStateExited:
                # Early-exit if we are in 'boring' states
                pass
            else:
                while not done and self.processListener is not None:
                    if not self.processListener.PeekAtNextEvent(event):
                        if wait_seconds > 0:
                            # No events on the queue, but we are allowed to wait for wait_seconds
                            # for any events to show up.
                            self.processListener.WaitForEvent(wait_seconds, event)
                            new_state = lldb.SBProcess.GetStateFromEvent(event)

                            num_events_handled += 1

                        done = not self.processListener.PeekAtNextEvent(event)
                    else:
                        # An event is on the queue, process it here.
                        self.processListener.GetNextEvent(event)
                        new_state = lldb.SBProcess.GetStateFromEvent(event)

                        # continue if stopped after attaching
                        if (
                            old_state == lldb.eStateAttaching
                            and new_state == lldb.eStateStopped
                        ):
                            self.process.Continue()

                        # If needed, perform any event-specific behaviour here
                        num_events_handled += 1

        if num_events_handled == 0:
            pass
        else:
            if old_state == new_state:
                status = ""
            self.ui.update(self.target, status, self, goto_file)


def returnCompleteCommand(a, l, p):
    """ Returns a "\n"-separated string with possible completion results
      for command a with length l and cursor at p.
    """
    logging.debug("returnCompleteCommand")
    separator = "\n"
    results = ctrl.completeCommand(a, l, p)
    vim.command('return "%s%s"' % (separator.join(results), separator))


def returnCompleteWindow(a, l, p):
    """ Returns a "\n"-separated string with possible completion results
      for commands that expect a window name parameter (like hide/show).
      FIXME: connect to ctrl.ui instead of hardcoding the list here
    """
    logging.debug("returnCompleteWindow")
    separator = "\n"
    results = [
        "breakpoints",
        "backtrace",
        "disassembly",
        "locals",
        "threads",
        "registers",
    ]
    vim.command('return "%s%s"' % (separator.join(results), separator))


global ctrl
ctrl = LLDBController()
