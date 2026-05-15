from lupa import LuaRuntime

class ScriptEngine:
    def __init__(self, registry: TagRegistry):
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        self.registry = registry
        self._script = ""
        self._env = {
            "tag_get": lambda n: self.registry.tags[n].value if n in self.registry.tags else None,
            "tag_set": lambda n, v: self.registry.update_tag(n, v),
        }

    def load_script(self, path: str):
        with open(path, "r") as f:
            self._script = f.read()
        self._func = self.lua.eval(self._script)

    def execute(self):
        try:
            self._func(**self._env)
        except Exception as e:
            print(f"[SCRIPT] Error: {e}")