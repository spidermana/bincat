# -*- coding: utf-8 -*-
# version IDA 6.9
# runs the "bincat" command from ida
"""
    This file is part of BinCAT.
    Copyright 2014-2017 - Airbus Group

    BinCAT is free software: you can redistribute it and/or modify
    it under the terms of the GNU Affero General Public License as published by
    the Free Software Foundation, either version 3 of the License, or (at your
    option) any later version.

    BinCAT is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU Affero General Public License for more details.

    You should have received a copy of the GNU Affero General Public License
    along with BinCAT.  If not, see <http://www.gnu.org/licenses/>.
"""

import collections
import hashlib
import logging
import os
import shutil
import sys
import tempfile
import traceback
import zlib
# Ugly but IDA Python Linux doesn't have it !
try:
    import distutils.spawn
    no_spawn = False
except ImportError:
    no_spawn = True
try:
    import requests
except ImportError:
    # log message will be displayed later
    pass

import idc
import idaapi
from idaapi import NW_OPENIDB, NW_CLOSEIDB, NW_TERMIDA, NW_REMOVE
import idabincat.netnode
import idabincat.npkgen
from idabincat.plugin_options import PluginOptions
from idabincat.analyzer_conf import AnalyzerConfig, AnalyzerConfigurations
from idabincat.gui import GUI

from PyQt5 import QtCore
# used in idaapi
from PyQt5 import QtWidgets

# Logging
logging.basicConfig(level=logging.DEBUG)
bc_log = logging.getLogger('bincat.plugin')
bc_log.setLevel(logging.DEBUG)


def dedup_loglines(loglines, max=None):
    res = []
    staging = None
    n = 0

    def flush_staging():
        if n > 0:
            res.append(staging)
            if max and len(res) >= max:
                return True
        if n == 2:
            res.append(staging)
            if max and len(res) >= max:
                return True
        if n > 2:
            res.append("== (same line repeated %i times) ==" % n)
            if max and len(res) >= max:
                return True

    while True:
        if not loglines:
            flush_staging()
            break
        l = loglines.pop()
        if l == staging:
            n += 1
        else:
            if flush_staging():
                break
            staging = l
            n = 1
    res.reverse()
    return res


class AnalyzerUnavailable(Exception):
    pass


class BincatPlugin(idaapi.plugin_t):
    # variables required by IDA
    flags = 0  # normal plugin
    wanted_name = "BinCAT"
    wanted_hotkey = "Ctrl-Shift-B"
    comment = "Interface to the BinCAT analyzer"
    help = ""
    initialized = False

    def __init__(self):
        super(BincatPlugin, self).__init__()
        self.state = None

    # IDA API methods: init, run, term
    def init(self):
        bc_log.debug("BinCAT init")
        info = idaapi.get_inf_structure()
        # IDA 6/7 compat
        procname = info.procname if hasattr(info, 'procname') else info.get_proc_name()[0]
        if procname != 'metapc':
            bc_log.info("Not on x86, not loading BinCAT")
            return idaapi.PLUGIN_SKIP
        try:
            from pybincat import cfa as cfa_module
            global cfa_module
        except:
            bc_log.warning(
                "Failed to load 'pybincat.cfa' python module\n%s",
                repr(sys.exc_info()))
            return idaapi.PLUGIN_SKIP
        PluginOptions.init()

        if no_spawn:
            bc_exe = None
        else:
            # Check if bincat_native is available
            bc_exe = distutils.spawn.find_executable('bincat_native')
        if bc_exe is None and os.name == 'nt':
            # add to PATH
            userdir = idaapi.get_user_idadir()
            bin_path = os.path.join(userdir, "plugins", "idabincat", "bin")
            if os.path.isdir(bin_path):
                os.environ['PATH'] += ";"+bin_path
            if no_spawn:
                bc_exe = os.path.join(bin_path, "bincat_native.exe")
            else:
                bc_exe = distutils.spawn.find_executable('bincat_native')
        if bc_exe is None and no_spawn is False:
            bc_log.warning('Could not find bincat_native binary, will not be able to run analysis')

        if PluginOptions.get("autostart") != "True":
            # will initialize later
            return idaapi.PLUGIN_OK

        bc_log.info("Autostarting")

        self.state = State()
        self.initialized = True
        bc_log.info("IDABinCAT ready.")
        return idaapi.PLUGIN_KEEP

    def run(self, args):
        if self.initialized:
            return
        self.state = State()
        # XXX move
        self.initialized = True
        self.state.gui.show_windows()
        bc_log.info("IDABinCAT ready.")

    def term(self):
        if self.state:
            bc_log.debug("Terminating BinCAT")
            self.state.clear_background()
            self.state.gui.term()
            self.state.gui = None
            self.state = None


class Analyzer(object):
    def __init__(self, path, finish_cb):
        self.path = path
        self.finish_cb = finish_cb

    def generate_tnpk(self, fname=None, destfname=None):
        """
        Generates TNPK file for provided fname. If None, generate one for the
        binary that is currently being analyzed in IDA, using IDA-provided
        headers.

        Returns file path to generated tnpk (string), or None if generation was
        not successful.
        """
        return None

    @property
    def initfname(self):
        return os.path.join(self.path, "init.ini")

    @property
    def outfname(self):
        return os.path.join(self.path, "out.ini")

    @property
    def cfainfname(self):
        return os.path.join(self.path, "cfain.marshal")

    @property
    def cfaoutfname(self):
        return os.path.join(self.path, "cfaout.marshal")

    @property
    def logfname(self):
        return os.path.join(self.path, "analyzer.log")


class LocalAnalyzer(Analyzer, QtCore.QProcess):
    """
    Runs BinCAT locally using QProcess.
    """

    def __init__(self, *args, **kwargs):
        QtCore.QProcess.__init__(self)
        Analyzer.__init__(self, *args, **kwargs)
        # Qprocess signal handlers
        self.error.connect(self.procanalyzer_on_error)
        self.stateChanged.connect(self.procanalyzer_on_state_change)
        self.started.connect(self.procanalyzer_on_start)
        self.finished.connect(self.procanalyzer_on_finish)

    def generate_tnpk(self, fname=None, destfname=None):
        if fname:
            imports_data = open(fname, 'rb').read()
        else:
            imports_data = ""
        try:
            npk_fname = idabincat.npkgen.NpkGen().generate_tnpk(
                imports_data=imports_data, destfname=destfname)
            return npk_fname
        except idabincat.npkgen.NpkGenException as e:
            return

    def run(self):
        cmdline = "bincat_native %s %s %s" % (self.initfname, self.outfname,
                                              self.logfname)
        # start the process
        bc_log.debug("Analyzer cmdline: [%s]", cmdline)
        try:
            self.start(cmdline)
        except Exception as e:
            bc_log.error("BinCAT failed to launch the analyzer.py")
            bc_log.warning("Exception: %s\n%s", str(e), traceback.format_exc())
        else:
            bc_log.info("Analyzer started.")

    def procanalyzer_on_error(self, err):
        errors = ["Failed to start", "Crashed", "TimedOut", "Read Error",
                  "Write Error", "Unknown Error"]
        try:
            errtxt = errors[err]
        except IndexError:
            errtxt = "Unspecified error %s" % err
        bc_log.error("Analyzer error: %s", errtxt)
        self.process_output()

    def procanalyzer_on_state_change(self, new_state):
        states = ["Not running", "Starting", "Running"]
        bc_log.debug("Analyzer new state: %s", states[new_state])

    def procanalyzer_on_start(self):
        bc_log.info("Analyzer: starting process")

    def procanalyzer_on_finish(self):
        bc_log.info("Analyzer process finished")
        exitcode = self.exitCode()
        bc_log.error("analyzer returned exit code=%i", exitcode)
        self.process_output()

    def process_output(self):
        """
        Try to process analyzer output.
        """
        bc_log.info("---- stdout ----------------")
        bc_log.info(str(self.readAllStandardOutput()))
        bc_log.info("---- stderr ----------------")
        bc_log.info(str(self.readAllStandardError()))
        bc_log.debug("---- logfile ---------------")
        if os.path.exists(self.logfname):
            with open(self.logfname, 'rb') as f:
                log_lines = f.readlines()
            log_lines = dedup_loglines(log_lines, max=100)
            if len(log_lines) > 100:
                bc_log.debug("---- Only the last 100 log lines (deduped) are displayed here ---")
                bc_log.debug("---- See full log in %s ---" % self.logfname)
            for line in log_lines:
                bc_log.debug(line.rstrip())

        bc_log.debug("====== end of logfile ======")
        self.finish_cb(self.outfname, self.logfname, self.cfaoutfname)


class WebAnalyzer(Analyzer):
    API_VERSION = "1.2"

    def __init__(self, *args, **kwargs):
        Analyzer.__init__(self, *args, **kwargs)
        self.server_url = PluginOptions.get("server_url").rstrip("/")
        self.reachable_server = False
        self.check_version()  # raises exception if server is unreachable
        self.reachable_server = True

    def check_version(self):
        try:
            version_req = requests.get(self.server_url + "/version")
            srv_api_version = str(version_req.text)
        except:
            raise AnalyzerUnavailable(
                "BinCAT server at %s could not be reached" % self.server_url)
        if srv_api_version != WebAnalyzer.API_VERSION:
            raise AnalyzerUnavailable(
                "API mismatch: this plugin supports version %s, while server "
                "supports version %s." % (WebAnalyzer.API_VERSION,
                                          srv_api_version))
        return True

    def generate_tnpk(self, fname=None, destfname=None):
        if not self.reachable_server:
            return
        if not fname:
            headers_data = idabincat.npkgen.NpkGen().get_header_data()
            fname = os.path.join(self.path, "ida_generated_headers.h")
            with open(fname, 'wb') as f:
                f.write(headers_data)
        sha256 = self.sha256_digest(fname)
        if not self.upload_file(fname, sha256):
            return
        npk_res = requests.post(self.server_url + "/convert_to_tnpk/" + sha256)
        if npk_res.status_code != 200:
            bc_log.error("Error while compiling file to tnpk "
                         "on BinCAT analysis server.")
            return
        res = npk_res.json()
        if 'status' not in res or res['status'] != 'ok':
            return
        sha256 = res['sha256']
        npk_contents = self.download_file(sha256)
        npk_fname = os.path.join(self.path, "headers.npk")
        with open(npk_fname, 'wb') as f:
            f.write(npk_contents)
        return npk_fname

    def run(self):
        if not self.reachable_server:
            return
        if 'requests' not in sys.modules:
            bc_log.error("python module 'requests' could not be imported, "
                         "so remote BinCAT cannot be used.")
            return
        # create temporary AnalyzerConfig to replace referenced file names with
        # sha256 of their contents
        with open(self.initfname, 'rb') as f:
            temp_config = AnalyzerConfig.load_from_str(f.read())
        # patch filepath - set filepath to sha256, upload file
        sha256 = self.sha256_digest(temp_config.binary_filepath)
        if not self.upload_file(temp_config.binary_filepath, sha256):
            return
        temp_config.binary_filepath = sha256
        # patch [imports] headers - replace with sha256, upload files
        files = temp_config.headers_files.split(',')
        h_shalist = []
        for f in files:
            if not f:
                continue
            try:
                f_sha256 = self.sha256_digest(f)
                h_shalist.append(f_sha256)
            except IOError as e:
                bc_log.error("Could not open file %s" % f, exc_info=1)
                return
            if not self.upload_file(f, f_sha256):
                bc_log.error("Could not upload file %s" % f, exc_info=1)
                return
        temp_config.headers_files = ','.join(h_shalist)

        # patch in_marshalled_cfa_file - replace with file contents sha256
        if os.path.exists(self.cfainfname):
            cfa_sha256 = self.sha256_digest(self.cfainfname)
            temp_config.in_marshalled_cfa_file = cfa_sha256
        else:
            temp_config.in_marshalled_cfa_file = "no-input-file"
        temp_config.out_marshalled_cfa_file = "cfaout.marshal"
        # write patched config file
        init_ini_str = str(temp_config)
        # --- Run analysis
        run_res = requests.post(
            self.server_url + "/analyze",
            files={'init.ini': ('init.ini', init_ini_str)})
        if run_res.status_code != 200:
            bc_log.error("Error while uploading analysis configuration file "
                         "to BinCAT analysis server (%r)" % run_res.text)
            return
        files = run_res.json()
        if files["errorcode"]:
            bc_log.error("Error while analyzing file. Bincat output is:\n"
                         "----------------\n%s\n----------------"
                         % self.download_file(files["stdout.txt"]))
            if not files["out.ini"]:  # try to parse out.ini if it exists
                return
        with open(self.outfname, 'wb') as outfp:

            outfp.write(self.download_file(files["out.ini"]))
        analyzer_log_contents = self.download_file(files["analyzer.log"])
        with open(self.logfname, 'wb') as logfp:
            logfp.write(analyzer_log_contents)
        bc_log.info("---- stdout+stderr ----------------")
        bc_log.info(self.download_file(files["stdout.txt"]))
        bc_log.debug("---- logfile ---------------")
        log_lines = analyzer_log_contents.split('\n')

        log_lines = dedup_loglines(log_lines, max=100)
        if len(log_lines) > 100:
            bc_log.debug("---- Only the last 100 log lines (deduped) are displayed here ---")
            bc_log.debug("---- See full log in %s ---" % self.logfname)
        for line in log_lines:
            bc_log.debug(line.rstrip())

        bc_log.debug("----------------------------")
        if "cfaout.marshal" in files:
            # might be absent (ex. when analysis failed, or not requested)
            with open(self.cfaoutfname, 'wb') as cfaoutfp:
                cfaoutfp.write(self.download_file(files["cfaout.marshal"]))
        self.finish_cb(self.outfname, self.logfname, self.cfaoutfname)

    def download_file(self, fname):
        r = requests.get(self.server_url + '/download/' + fname + '/zlib')
        try:
            return zlib.decompress(r.content)
        except Exception as e:
            bc_log.error("Error uncompressing downloaded file [%s] (%s)" %
                         (fname, e))

    def sha256_digest(self, path):
        with open(path, 'rb') as f:
            h = hashlib.new('sha256')
            h.update(f.read())
            sha256 = h.hexdigest().lower()
        return sha256

    def upload_file(self, path, sha256):
        # check whether file has already been uploaded
        try:
            check_res = requests.head(
                self.server_url + "/download/%s" % sha256)
        except requests.exceptions.ConnectionError as e:
            bc_log.error("Error when contacting the BinCAT server: %s", e)
            return
        if check_res.status_code == 404:
            # Confirmation dialog before uploading file?
            msgBox = QtWidgets.QMessageBox()
            msgBox.setText("Do you really want to upload %s to %s?" %
                           (path, self.server_url))
            msgBox.setStandardButtons(QtWidgets.QMessageBox.Yes |
                                      QtWidgets.QMessageBox.No)
            msgBox.setIcon(QtWidgets.QMessageBox.Question)
            ret = msgBox.exec_()
            if ret == QtWidgets.QMessageBox.No:
                bc_log.info("Upload aborted.")
                return
            # upload
            upload_res = requests.put(
                self.server_url + "/add",
                files={'file': ('file', open(path, 'rb').read())})
            if upload_res.status_code != 200:
                bc_log.error("Error while uploading file %s "
                             "to BinCAT analysis server.", path)
                return
        return True


class State(object):
    """
    Container for (static) plugin state related data & methods.
    """
    def __init__(self):
        self.current_ea = None
        self.cfa = None
        self.current_state = None
        self.current_node_ids = []
        #: last run config
        self.current_config = None
        #: config to be edited
        self.edit_config = None
        #: Analyzer instance - protects against merciless garbage collector
        self.analyzer = None
        self.hooks = None
        self.netnode = idabincat.netnode.Netnode("$ com.bincat.bcplugin")
        #: acts as a List of ("eip", "register name", "taint mask")
        #: XXX store in IDB?
        self.overrides = CallbackWrappedList()
        #: list of (name, config)
        self.configurations = AnalyzerConfigurations(self)
        # XXX store in idb after encoding?
        self.last_cfaout_marshal = None
        #: filepath to last dumped remapped binary
        self.remapped_bin_path = None
        self.remap_binary = True
        # for debugging purposes, to interact with this object from the console
        global bc_state
        bc_state = self

        self.gui = GUI(self)
        if PluginOptions.get("load_from_idb") == "True":
            self.load_from_idb()

    def new_analyzer(self, *args, **kwargs):
        """
        returns current Analyzer class (web or local)
        """
        if (PluginOptions.get("web_analyzer") == "True" and
                PluginOptions.get("server_url") != ""):
            return WebAnalyzer(*args, **kwargs)
        else:
            return LocalAnalyzer(*args, **kwargs)

    def load_from_idb(self):
        if "out.ini" in self.netnode and "analyzer.log" in self.netnode:
            bc_log.info("Loading analysis results from idb")
            path = tempfile.mkdtemp(suffix='bincat')
            outfname = os.path.join(path, "out.ini")
            logfname = os.path.join(path, "analyzer.log")
            with open(outfname, 'wb') as outfp:
                outfp.write(self.netnode["out.ini"])
            with open(logfname, 'wb') as logfp:
                logfp.write(self.netnode["analyzer.log"])
            if "current_ea" in self.netnode:
                ea = self.netnode["current_ea"]
            else:
                ea = None
            self.analysis_finish_cb(outfname, logfname, cfaoutfname=None,
                                    ea=ea)
        if "remapped_bin_path" in self.netnode:
            fname = self.netnode["remapped_bin_path"]
            if os.path.isfile(fname):
                self.remapped_bin_path = fname
        if "remap_binary" in self.netnode:
            self.remap_binary = self.netnode["remap_binary"]

    def clear_background(self):
        """
        reset background color for previous analysis
        """
        if self.cfa:
            color = idaapi.calc_bg_color(idaapi.NIF_BG_COLOR)
            for v in self.cfa.states:
                ea = v.value
                idaapi.set_item_color(ea, color)

    def analysis_finish_cb(self, outfname, logfname, cfaoutfname, ea=None):
        bc_log.debug("Parsing analyzer result file")
        try:
            cfa = cfa_module.CFA.parse(outfname, logs=logfname)
        except (pybincat.PyBinCATException):
            bc_log.error("Could not parse result file")
            return None
        self.clear_background()
        self.cfa = cfa
        if cfa:
            # XXX add user preference for saving to idb? in that case, store
            # reference to marshalled cfa elsewhere
            bc_log.info("Storing analysis results to idb")
            with open(outfname, 'rb') as f:
                self.netnode["out.ini"] = f.read()
            with open(logfname, 'rb') as f:
                self.netnode["analyzer.log"] = f.read()
            if self.remapped_bin_path:
                self.netnode["remapped_bin_path"] = self.remapped_bin_path
            self.netnode["remap_binary"] = self.remap_binary
            if cfaoutfname is not None and os.path.isfile(cfaoutfname):
                with open(cfaoutfname, 'rb') as f:
                    self.last_cfaout_marshal = f.read()
        else:
            bc_log.info("Empty or unparseable result file.")
        bc_log.debug("----------------------------")
        # Update current RVA to start address (nodeid = 0)
        # by default, use current ea - e.g, in case there is no results (cfa is
        # None) or no node 0 (happens in backward mode)
        current_ea = self.current_ea
        if ea is not None:
            current_ea = ea
        else:
            try:
                node0 = cfa['0']
                if node0:
                    current_ea = node0.address.value
            except (KeyError, TypeError):
                # no cfa is None, or no node0
                pass
        self.set_current_ea(current_ea, force=True)
        self.netnode["current_ea"] = current_ea
        if not cfa:
            return
        for addr, nodeids in cfa.states.items():
            ea = addr.value
            tainted = False
            for n_id in nodeids:
                # is it tainted?
                # find children state
                state = cfa[n_id]
                if state.tainted:
                    tainted = True
                    break

            if tainted:
                idaapi.set_item_color(ea, 0xDDFFDD)
            else:
                idaapi.set_item_color(ea, 0xCDCFCE)

    def set_current_node(self, node_id):
        if self.cfa:
            state = self.cfa[node_id]
            if state:
                self.set_current_ea(state.address.value,
                                    force=True, node_id=node_id)

    def set_current_ea(self, ea, force=False, node_id=None):
        """
        :param ea: int or long
        """
        if not (force or ea != self.current_ea):
            return
        self.gui.before_change_ea()
        self.current_ea = ea
        nonempty_state = False
        if self.cfa:
            node_ids = self.cfa.node_id_from_addr(ea)
            if node_ids:
                nonempty_state = True
                if node_id in node_ids:
                    self.current_state = self.cfa[node_id]
                else:
                    self.current_state = self.cfa[node_ids[0]]
                self.current_node_ids = node_ids
        if not nonempty_state:
            self.current_state = None
            self.current_node_ids = []

        self.gui.after_change_ea()

    def guess_filepath(self):
        filepath = self.current_config.binary_filepath
        if os.path.isfile(filepath):
            return filepath
        # try to use idaapi.get_input_file_path
        filepath = idaapi.get_input_file_path()
        if os.path.isfile(filepath):
            return filepath
        # get_input_file_path returns file path from IDB, which may not
        # exist locally if IDB has been moved (eg. send idb+binary to
        # another analyst)
        filepath = idc.GetIdbPath().replace('idb', 'exe')
        if os.path.isfile(filepath):
            return filepath
        # give up
        return None

    def start_analysis(self, config_str=None):
        """
        Creates new temporary dir. File structure:
        input files: init.ini, cfain.marshal
        output files: out.ini, cfaout.marshal
        """
        if config_str:
            self.current_config = AnalyzerConfig.load_from_str(config_str)
        binary_filepath = self.guess_filepath()
        if not binary_filepath:
            bc_log.error(
                "File %s does not exit. Please fix path in configuration.",
                self.current_config.binary_filepath)
            return
        bc_log.debug("Using %s as source binary path", binary_filepath)
        self.current_config.binary_filepath = binary_filepath

        path = tempfile.mkdtemp(suffix='bincat')

        # instance variable: we don't want the garbage collector to delete the
        # *Analyzer instance, killing an unlucky QProcess in the process
        try:
            self.analyzer = self.new_analyzer(path, self.analysis_finish_cb)
        except AnalyzerUnavailable as e:
            bc_log.error("Analyzer is unavailable", exc_info=True)
            return

        bc_log.debug("Current analyzer path: %s", path)

        # Update overrides
        self.current_config.update_overrides(self.overrides)
        analysis_method = self.current_config.analysis_method
        if analysis_method in ("forward_cfa", "backward"):
            if self.last_cfaout_marshal is None:
                bc_log.error("No marshalled CFA has been recorded - run a "
                             "forward analysis first.")
                return
            with open(self.analyzer.cfainfname, 'wb') as f:
                f.write(self.last_cfaout_marshal)
        # Set correct file names
        # Note: bincat_native expects a filename for in_marshalled_cfa_file -
        # may not exist if analysis mode is forward_binary
        self.current_config.set_cfa_options('true', self.analyzer.cfainfname,
                                            self.analyzer.cfaoutfname)
        bc_log.debug("Generating .no files...")

        headers_filenames = self.current_config.headers_files.split(',')
        # compile .c files for libs, if there are any
        bc_log.debug("Initial header files: %r", headers_filenames)
        new_headers_filenames = []
        for f in headers_filenames:
            f = f.strip()
            if not f:
                continue
            if f.endswith('.c'):
                # generate .npk from .c file
                if not os.path.isfile(f):
                    bc_log.warning(
                        "header file %s could not be found, continuing", f)
                    continue
                new_npk_fname = f[:-2] + '.no'
                headers_filenames.remove(f)
                if not os.path.isfile(new_npk_fname):
                    # compile
                    self.analyzer.generate_tnpk(fname=f,
                                                destfname=new_npk_fname)
                    if not os.path.isfile(new_npk_fname):
                        bc_log.warning(
                            ".no file containing type data for the headers "
                            "file %s could not be generated, continuing", f)
                        continue
                    f = new_npk_fname
            # Relative paths are copied
            elif f.endswith('.no') and os.path.isfile(f):
                if f[0] != os.path.sep:
                    temp_npk_fname = os.path.join(path, os.path.basename(f))
                    shutil.copyfile(f, temp_npk_fname)
                    f = temp_npk_fname
            else:
                bc_log.warning(
                    "Header file %s does not match expected extensions "
                    "(.c, .no), ignoring.", f)
                continue

            new_headers_filenames.append(f)
        headers_filenames = new_headers_filenames
        # generate npk file for the binary being analyzed (unless it has
        # already been generated)
        if not any(
                [s.endswith('pre-processed.no') for s in headers_filenames]):
            npk_filename = self.analyzer.generate_tnpk()
            if not npk_filename:
                bc_log.warning(
                    ".no file containing type data for the file being "
                    "analyzed could not be generated, continuing. The "
                    "ida-generated header could be invalid.")
            else:
                headers_filenames.append(npk_filename)
            bc_log.debug("Final npk files: %r" % headers_filenames)
        self.current_config.headers_files = ','.join(headers_filenames)

        self.current_config.write(self.analyzer.initfname)
        self.analyzer.run()

    def re_run(self):
        """
        Re-run analysis, taking new overrides settings into account
        """
        if self.start_analysis is None:
            # XXX upload all required files in Web Analyzer
            # XXX Store all required files in IDB
            bc_log.error(
                "You have to run the analysis first using the 'Analyze From "
                "here (Ctrl-Shift-A)' menu - reloading previous results from "
                "IDB is not yet supported.")
        else:
            self.start_analysis()


class CallbackWrappedList(collections.MutableSequence):
    """
    Acts as a List object, wraps write access with calls to properly invalidate
    models associated with View GUI objects.
    Should store only immutable objects.
    """
    def __init__(self):
        self._data = []
        #: list of functions to be called prior to updating list
        self.pre_callbacks = []
        #: list of functions to be called after updating list
        self.post_callbacks = []
        #: cache

    def register_callbacks(self, pre_cb, post_cb):
        if pre_cb:
            self.pre_callbacks.append(pre_cb)
        if post_cb:
            self.post_callbacks.append(post_cb)

    def __getitem__(self, index):
        # no need for wrappers - values should be immutable
        return self._data[index]

    def _callback_wrap(f):
        def wrap(self, *args, **kwargs):
            for cb in self.pre_callbacks:
                cb()
            f(self, *args, **kwargs)
            for cb in self.post_callbacks:
                cb()
        return wrap

    @_callback_wrap
    def __setitem__(self, index, value):
        self._data[index] = tuple(value)

    @_callback_wrap
    def __delitem__(self, item):
        del self._data[item]

    def __len__(self):
        return len(self._data)

    @_callback_wrap
    def insert(self, index, value):
        self._data.insert(index, tuple(value))


def PLUGIN_ENTRY():
    return BincatPlugin()
