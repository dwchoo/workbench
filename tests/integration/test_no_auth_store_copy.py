"""WBX-B12 / C-D64: no repo code copies the OMP auth store (``agent.db``).

OAuth refresh tokens rotate on use: an OMP started on a *copy* of the store spends the original's refresh token
and keeps the new one in the copy, so the user's own login breaks. Helpers share the store with a symlink, like
the Workbench OMP homes (``omp_home.prepare_omp_home``).

Static check over every ``.py`` under ``src``, ``tests`` and ``tools``, per function (and module body), with a
simple intra-function data flow. A *store path* is an expression naming ``agent.db`` (a string literal, an f-string
part, a folded ``'agent' + '.db'``, a module-level constant holding one, ``AUTH_STORE_NAME``, a call to a function
named ``*auth_store*``), a variable assigned from one, or a file opened on one (``with open(...) as f``); ``.parent``
/ ``dirname`` of a store path is its directory, not the store. *Store content* is what a read of a store path returns
(``.read()``, ``read_bytes``/``read_text``, ``os.read``/``pread``) and variables assigned from it. Flagged:
- a copy primitive with a store path operand: ``shutil.copy*``/``copytree``/``move``, ``os.sendfile``/
  ``copy_file_range``/``link``, SQLite ``.backup`` (import aliases included);
- a write (``write``, ``write_bytes``, ``write_text``, ``writelines``, ``os.write``/``pwrite``) of store content;
- ``subprocess.*``/``os.system``/``os.popen``/``os.exec*``/``os.spawn*`` with a store path and a copy tool
  (``cp``, ``rsync``, ``install``, ``dd``, ``cat``, ``sqlite3``, ``tar``, ``scp``) in its arguments.
It cannot see: copies made by a helper that receives the path as a parameter from another function or module
(no inter-procedural flow), paths built without the literal name (``Path.home() / '.omp' / 'agent' / name`` with
``name`` from input or the environment), ``getattr``/``exec``/``eval`` tricks, SQL such as ``VACUUM INTO``,
copies through third-party libraries, and non-Python files (shell scripts, Makefiles).
"""
import ast
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCANNED = ("src", "tests", "tools")
AUTH_NAMES = ("agent.db",)
AUTH_IDENTIFIERS = ("AUTH_STORE_NAME",)
COPY_ATTRIBUTES = {"shutil": {"copy", "copy2", "copyfile", "copyfileobj", "copytree", "move"},
                   "os": {"sendfile", "copy_file_range", "link"}}
WRITE_ATTRIBUTES = {"write", "write_bytes", "write_text", "writelines"}
READ_ATTRIBUTES = {"read", "read_bytes", "read_text", "readlines", "readline", "pread"}
SHELL_CALLS = {"subprocess": {"run", "call", "check_call", "check_output", "Popen", "getoutput", "getstatusoutput"},
               "os": {"system", "popen", "execv", "execvp", "execl", "execlp", "spawnv", "spawnvp", "posix_spawn",
                      "posix_spawnp"}}
COPY_TOOL = re.compile(r"(?:^|[\s/;&|])(?:cp|rsync|install|dd|cat|sqlite3|tar|scp)(?:\s|$)")
DIRECTORY_ATTRIBUTES = {"parent", "parents", "anchor"}
DIRECTORY_CALLS = {"dirname"}


class _Module:
    """Import aliases and module-level store-path constants of one file."""

    def __init__(self, tree: ast.Module):
        self.modules: dict[str, str] = {}  # local name -> shutil/os/subprocess
        self.functions: dict[str, tuple[str, str]] = {}  # local name -> (module, attribute)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in ("shutil", "os", "subprocess"):
                        self.modules[alias.asname or alias.name] = alias.name
            elif isinstance(node, ast.ImportFrom) and node.module in ("shutil", "os", "subprocess"):
                for alias in node.names:
                    self.functions[alias.asname or alias.name] = (node.module, alias.name)
        self.paths: set[str] = set()
        _Scope(self, list(_own_nodes(tree)), self.paths).flow()

    def target(self, func) -> tuple[str, str] | None:
        """``(module, attribute)`` a call goes to, through import aliases."""
        if isinstance(func, ast.Name):
            return self.functions.get(func.id)
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id in self.modules:
            return self.modules[func.value.id], func.attr
        return None


def _own_nodes(scope):
    """The nodes of ``scope`` without those of nested functions (they are scanned on their own)."""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def _strings(node) -> list[str]:
    """String literals in ``node``, with ``+`` of literals folded and f-string parts joined."""
    found = []
    for item in ast.walk(node):
        if isinstance(item, ast.Constant) and isinstance(item.value, str):
            found.append(item.value)
        elif isinstance(item, ast.JoinedStr):
            found.append("".join(part.value for part in item.values
                                 if isinstance(part, ast.Constant) and isinstance(part.value, str)))
        elif isinstance(item, ast.BinOp) and isinstance(item.op, ast.Add):
            folded = _fold(item)
            if folded is not None:
                found.append(folded)
    return found


def _fold(node) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _fold(node.left), _fold(node.right)
        return None if left is None or right is None else left + right
    return None


def _flows(nodes):
    """(target, value) pairs of the assignments, ``with ... as`` items and ``for`` loops among ``nodes``."""
    for node in nodes:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                yield target, node.value
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)) and node.value is not None:
            yield node.target, node.value
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            yield node.optional_vars, node.context_expr
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            yield node.target, node.iter


class _Scope:
    def __init__(self, module: _Module, nodes: list, paths: set[str] | None = None):
        self.module, self.nodes = module, nodes
        self.paths = paths if paths is not None else set(module.paths)
        self.content: set[str] = set()

    def is_path(self, node) -> bool:
        """Whether ``node`` names the store (not merely its directory)."""
        if isinstance(node, ast.Attribute) and node.attr in DIRECTORY_ATTRIBUTES:
            return False
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute) \
                and node.value.attr in DIRECTORY_ATTRIBUTES:
            return False
        if isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if name in DIRECTORY_CALLS:
                return False
            if "auth_store" in name:
                return True
            if name in READ_ATTRIBUTES:  # what a read returns is content, not a path
                return False
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return any(name in node.value for name in AUTH_NAMES)
        if isinstance(node, (ast.JoinedStr, ast.BinOp)) and any(
                name in text for text in _strings(node) for name in AUTH_NAMES):
            return True
        if isinstance(node, ast.Name):
            return node.id in AUTH_IDENTIFIERS or node.id in self.paths
        if isinstance(node, ast.Attribute) and node.attr in AUTH_IDENTIFIERS:
            return True
        return any(self.is_path(child) for child in ast.iter_child_nodes(node))

    def is_content(self, node) -> bool:
        """Whether ``node`` carries what was read from the store."""
        for item in ast.walk(node):
            if isinstance(item, ast.Name) and item.id in self.content:
                return True
            if isinstance(item, ast.Call):
                func = item.func
                if isinstance(func, ast.Attribute) and func.attr in READ_ATTRIBUTES:
                    if self.is_path(func.value) or any(self.is_path(arg) for arg in item.args[:1]):
                        return True
                if self.module.target(func) in (("os", "read"), ("os", "pread")) and item.args \
                        and self.is_path(item.args[0]):
                    return True
        return False

    def flow(self) -> None:
        flows = list(_flows(self.nodes))
        changed = True
        while changed:
            changed = False
            for target, value in flows:
                names = {item.id for item in ast.walk(target) if isinstance(item, ast.Name)}
                for kind, test in ((self.paths, self.is_path), (self.content, self.is_content)):
                    if names - kind and test(value):
                        kind |= names
                        changed = True

    def copies(self) -> bool:
        self.flow()
        for node in self.nodes:
            if not isinstance(node, ast.Call):
                continue
            operands = list(node.args) + [keyword.value for keyword in node.keywords]
            target = self.module.target(node.func)
            func = node.func
            if target is not None and target[1] in COPY_ATTRIBUTES.get(target[0], ()):
                if any(self.is_path(operand) for operand in operands):
                    return True
            elif isinstance(func, ast.Attribute) and func.attr == "backup":  # sqlite3.Connection.backup
                if any(self.is_path(operand) for operand in operands + [func.value]):
                    return True
            if target is not None and target[1] in SHELL_CALLS.get(target[0], ()):
                texts = [text for operand in operands for text in _strings(operand)]
                if any(self.is_path(operand) for operand in operands) and any(COPY_TOOL.search(text)
                                                                              for text in texts):
                    return True
            written = operands[1:] if target in (("os", "write"), ("os", "pwrite")) else (
                operands if isinstance(func, ast.Attribute) and func.attr in WRITE_ATTRIBUTES else [])
            if any(self.is_content(operand) for operand in written):
                return True
        return False


def auth_store_copiers(root: Path = ROOT) -> list[str]:
    found = []
    for top in SCANNED:
        for path in sorted((root / top).rglob("*.py")):
            if any(part in {"__pycache__", ".venv"} for part in path.parts) or path.name == Path(__file__).name:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
            except (SyntaxError, UnicodeDecodeError):
                continue
            module = _Module(tree)
            scopes = [tree] + [node for node in ast.walk(tree)
                               if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
            for scope in scopes:
                if _Scope(module, list(_own_nodes(scope))).copies():
                    name = getattr(scope, "name", "<module>")
                    found.append(f"{path.relative_to(root)}:{getattr(scope, 'lineno', 1)}:{name}")
    return found


class NoAuthStoreCopyTests(unittest.TestCase):
    def test_no_code_copies_the_omp_auth_store(self):
        self.assertEqual(auth_store_copiers(), [])

    def test_the_check_finds_a_copier(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "tests").mkdir()
            (root / "tests" / "helper.py").write_text(
                "import shutil\n"
                "def stage(source, root):\n"
                "    shutil.copyfile(source, root / 'agent/agent.db')\n"
                "def link(source, root):\n"
                "    (root / 'agent/agent.db').symlink_to(source)\n"
                "def placeholder(root):\n"
                "    shutil.copytree(root / 'src', root / 'snapshot')\n"
                "    (root / 'agent.db').write_bytes(b'')\n"
                "def snapshot(source, root):\n"
                "    import sqlite3\n"
                "    target = root / ('agent.db' + '')\n"
                "    with sqlite3.connect(source) as original, sqlite3.connect(target) as private:\n"
                "        original.backup(private)\n")
            self.assertEqual(auth_store_copiers(root), ["tests/helper.py:2:stage", "tests/helper.py:9:snapshot"])


if __name__ == "__main__":
    unittest.main()
