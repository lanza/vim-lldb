
# Try to import all dependencies, catch and handle the error gracefully if it fails.
import logging as lg
logging = lg.getLogger("vim-lldb")
logging.setLevel(lg.DEBUG)
fh = lg.FileHandler("/tmp/vim-lldb.log")
fh.setLevel(lg.DEBUG)
formatter = lg.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
fh.setFormatter(formatter)
logging.addHandler(fh)

import import_lldb

try:
    import lldb
    import vim
except ImportError:
    sys.stderr.write(
        """
        Unable to load vim/lldb module. Check lldb is on the path is available
        (or LLDB is set) and that script is invoked inside Vim with :pyfile
        """
    )
    pass
else:
    # Everthing went well, so use import to start the plugin controller
    from lldb_controller import *
