"""테스트는 항상 임시 DB를 쓴다(실제 data/app.db를 건드리지 않는다)."""
import atexit
import os
import shutil
import tempfile

_tmp = tempfile.mkdtemp(prefix="palacewalk-test-")
os.environ["DB_PATH"] = os.path.join(_tmp, "test.db")
atexit.register(shutil.rmtree, _tmp, ignore_errors=True)
