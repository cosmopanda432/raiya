"""raiya/config.py (設定ファイル config.yaml) の決定論 pin。
spec: docs/superpowers/specs/2026-10-23-config-file-design.md (§1 探索 / §2 スキーマ / §5.1 無効化の単位)。
"""
import ast
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

from raiya import config  # noqa: E402


class ConfigCase(unittest.TestCase):
    """一時ディレクトリの config.yaml を RAIYA_CONFIG に指す。_reset を前後で呼ぶ。"""

    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.path = os.path.join(self.root, "config.yaml")
        config._reset()
        self.addCleanup(config._reset)

    def write(self, text, path=None):
        with open(path or self.path, "w", encoding="utf-8") as f:
            f.write(text)
        return path or self.path

    def load(self, text):
        self.write(text)
        return config.load(self.path)

    def categories(self, cfg):
        return [(i.category, i.key) for i in cfg.issues]


class TestSearch(ConfigCase):
    def test_empty_string_disables_and_does_not_search(self):
        # XDG / HOME が有効な config を含んでいても、RAIYA_CONFIG="" なら使わない (§8 分離 pin)
        xdg = os.path.join(self.root, "xdg", "raiya")
        os.makedirs(xdg)
        self.write("inject_dispositions: true\n", os.path.join(xdg, "config.yaml"))
        home_cfg = os.path.join(self.root, "home", ".config", "raiya")
        os.makedirs(home_cfg)
        self.write("inject_dispositions: true\n", os.path.join(home_cfg, "config.yaml"))
        env = {"RAIYA_CONFIG": "", "XDG_CONFIG_HOME": os.path.join(self.root, "xdg"),
               "HOME": os.path.join(self.root, "home")}
        with mock.patch.dict(os.environ, env):
            cfg = config.get()
        self.assertIsNone(cfg.path)
        self.assertEqual(cfg.values, {})
        self.assertEqual(cfg.source, config.SRC_DISABLED)

    def test_suite_isolation_default(self):
        # tests/__init__.py が RAIYA_CONFIG="" を強制している
        self.assertEqual(os.environ.get("RAIYA_CONFIG"), "")
        self.assertIsNone(config.get().path)

    def test_explicit_path_expanduser(self):
        home = os.path.join(self.root, "home")
        os.makedirs(home)
        self.write("inject_dispositions: true\n", os.path.join(home, "c.yaml"))
        with mock.patch.dict(os.environ, {"RAIYA_CONFIG": "~/c.yaml", "HOME": home}):
            cfg = config.get()
        self.assertEqual(cfg.path, os.path.join(home, "c.yaml"))
        self.assertEqual(cfg.values, {"inject_dispositions": True})
        self.assertEqual(cfg.source, config.SRC_ENV)

    def test_explicit_relative_is_not_absolute(self):
        with mock.patch.dict(os.environ, {"RAIYA_CONFIG": "rel/config.yaml"}):
            cfg = config.get()
        self.assertEqual(self.categories(cfg), [("not_absolute", "")])
        self.assertTrue(cfg.whole_file)
        self.assertEqual(cfg.values, {})

    def test_explicit_missing_is_not_found_and_does_not_fall_back_to_xdg(self):
        xdg = os.path.join(self.root, "xdg", "raiya")
        os.makedirs(xdg)
        self.write("inject_dispositions: true\n", os.path.join(xdg, "config.yaml"))
        env = {"RAIYA_CONFIG": os.path.join(self.root, "nope.yaml"),
               "XDG_CONFIG_HOME": os.path.join(self.root, "xdg")}
        with mock.patch.dict(os.environ, env):
            cfg = config.get()
        self.assertEqual(self.categories(cfg), [("not_found", "")])
        self.assertTrue(cfg.whole_file)

    def test_xdg_used_when_env_unset(self):
        xdg = os.path.join(self.root, "xdg", "raiya")
        os.makedirs(xdg)
        self.write("inject_dispositions: true\n", os.path.join(xdg, "config.yaml"))
        env = {"XDG_CONFIG_HOME": os.path.join(self.root, "xdg")}
        with mock.patch.dict(os.environ, env):
            del os.environ["RAIYA_CONFIG"]
            cfg = config.get()
        self.assertEqual(cfg.values, {"inject_dispositions": True})
        self.assertEqual(cfg.source, config.SRC_XDG)

    def test_xdg_missing_file_is_silent(self):
        env = {"XDG_CONFIG_HOME": os.path.join(self.root, "xdg")}
        with mock.patch.dict(os.environ, env):
            del os.environ["RAIYA_CONFIG"]
            cfg = config.get()
        self.assertIsNone(cfg.path)
        self.assertEqual(cfg.issues, ())
        self.assertFalse(cfg.whole_file)

    def test_xdg_empty_or_relative_falls_back_to_home_config(self):
        home_cfg = os.path.join(self.root, "home", ".config", "raiya")
        os.makedirs(home_cfg)
        self.write("inject_dispositions: true\n", os.path.join(home_cfg, "config.yaml"))
        for xdg in ("", "relative/dir"):
            with self.subTest(xdg=xdg):
                config._reset()
                env = {"XDG_CONFIG_HOME": xdg, "HOME": os.path.join(self.root, "home")}
                with mock.patch.dict(os.environ, env):
                    del os.environ["RAIYA_CONFIG"]
                    cfg = config.get()
                self.assertEqual(cfg.path, os.path.join(home_cfg, "config.yaml"))
                self.assertEqual(cfg.values, {"inject_dispositions": True})
                self.assertEqual(cfg.source, config.SRC_XDG)

    def test_config_path_is_pure(self):
        self.assertIsNone(config.config_path({"RAIYA_CONFIG": ""}))
        self.assertEqual(config.config_path({"XDG_CONFIG_HOME": "/x"}), "/x/raiya/config.yaml")

    def test_project_dirs_are_never_searched(self):
        # cwd に config.yaml があっても読まない
        self.write("inject_dispositions: true\n", os.path.join(self.root, "config.yaml"))
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(self.root)
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": os.path.join(self.root, "x")}):
            del os.environ["RAIYA_CONFIG"]
            self.assertIsNone(config.get().path)


class TestLoadWholeFile(ConfigCase):
    def test_valid_full_file(self):
        cfg = self.load(
            "version: 1\nstate_dir: /var/raiya\ninject_dispositions: true\n"
            "llm:\n  backend: command\n  model: m\n  propose_model: pm\n  command: cat\n"
            "  timeout_sec: 12.5\n  openai_max_completion_tokens: 4096\n"
        )
        self.assertEqual(cfg.issues, ())
        self.assertFalse(cfg.whole_file)
        self.assertEqual(cfg.values, {
            "state_dir": "/var/raiya", "inject_dispositions": True, "llm.backend": "command",
            "llm.model": "m", "llm.propose_model": "pm", "llm.command": "cat",
            "llm.timeout_sec": 12.5, "llm.openai_max_completion_tokens": 4096,
        })

    def test_state_dir_expanduser(self):
        with mock.patch.dict(os.environ, {"HOME": "/home/x"}):
            cfg = self.load("state_dir: ~/.raiya\n")
        self.assertEqual(cfg.values["state_dir"], "/home/x/.raiya")

    def test_empty_file_and_null_values(self):
        for text in ("", "# only a comment\n", "state_dir: null\nllm:\n  model: null\n", "llm:\n"):
            with self.subTest(text=text):
                cfg = self.load(text)
                self.assertEqual((cfg.values, cfg.issues, cfg.whole_file), ({}, (), False))

    def test_unreadable_non_utf8(self):
        with open(self.path, "wb") as f:
            f.write(b"state_dir: \xff\xfe\n")
        cfg = config.load(self.path)
        self.assertEqual(self.categories(cfg), [("unreadable", "")])
        self.assertTrue(cfg.whole_file)

    def test_unreadable_directory(self):
        cfg = config.load(self.root)
        self.assertEqual(self.categories(cfg), [("unreadable", "")])

    def test_yaml_syntax_has_line(self):
        cfg = self.load("a: 1\nb: [unclosed\nc: 2\n")
        self.assertEqual([i.category for i in cfg.issues], ["yaml_syntax"])
        self.assertIsInstance(cfg.issues[0].line, int)
        self.assertTrue(cfg.whole_file)
        self.assertEqual(cfg.values, {})

    def test_unreadable_file_over_max_bytes(self):
        with open(self.path, "wb") as f:
            f.write(b"#" * (config.MAX_CONFIG_BYTES + 1))
        cfg = config.load(self.path)
        self.assertEqual(self.categories(cfg), [("unreadable", "")])
        self.assertTrue(cfg.issues[0].detail.startswith("too large (size="))
        self.assertTrue(cfg.whole_file)
        self.assertEqual(cfg.values, {})

    def test_unreadable_on_oserror_other_than_not_found(self):
        # FileNotFoundError 以外の OSError (権限等) は not_found ではなく unreadable
        self.write("inject_dispositions: true\n")
        with mock.patch("builtins.open", side_effect=PermissionError(13, "denied")):
            cfg = config.load(self.path)
        self.assertEqual(self.categories(cfg), [("unreadable", "")])
        self.assertTrue(cfg.whole_file)
        self.assertEqual(cfg.values, {})

    def test_yaml_syntax_without_problem_mark_has_no_line(self):
        import yaml
        self.write("a: 1\n")
        with mock.patch.object(yaml, "safe_load", side_effect=yaml.YAMLError("x")):
            cfg = config.load(self.path)
        self.assertEqual(self.categories(cfg), [("yaml_syntax", "")])
        self.assertIsNone(cfg.issues[0].line)
        self.assertTrue(cfg.whole_file)
        self.assertEqual(cfg.values, {})

    def test_yaml_missing(self):
        self.write("inject_dispositions: true\n")
        with mock.patch.dict(sys.modules, {"yaml": None}):
            cfg = config.load(self.path)
        self.assertEqual(self.categories(cfg), [("yaml_missing", "")])
        self.assertTrue(cfg.whole_file)

    def test_not_mapping(self):
        for text in ("- a\n- b\n", "just a string\n", "3\n"):
            with self.subTest(text=text):
                cfg = self.load(text)
                self.assertEqual(self.categories(cfg), [("not_mapping", "")])
                self.assertTrue(cfg.whole_file)

    def test_bad_version(self):
        for text in ("version: 2\n", "version: true\n", "version: '1'\n", "version: 1.0\n"):
            with self.subTest(text=text):
                cfg = self.load(text + "inject_dispositions: true\n")
                self.assertEqual(self.categories(cfg), [("bad_version", "")])
                self.assertTrue(cfg.whole_file)
                self.assertEqual(cfg.values, {})

    def test_version_omitted_or_1_ok(self):
        self.assertEqual(self.load("version: 1\n").issues, ())
        self.assertEqual(self.load("inject_dispositions: true\n").issues, ())


class TestPerKeySalvage(ConfigCase):
    def test_bad_key_drops_only_that_key(self):
        cfg = self.load(
            "state_dir: /keep\ninject_dispositions: true\nllm:\n  backnd: command\n  model: m\n"
        )
        self.assertFalse(cfg.whole_file)
        self.assertEqual(self.categories(cfg), [("unknown_key", "llm")])
        self.assertEqual(cfg.values, {"state_dir": "/keep", "inject_dispositions": True, "llm.model": "m"})

    def test_unknown_top_key(self):
        cfg = self.load("state_dir: /keep\nRAIYA_HOOKS: 0\n")
        self.assertEqual(self.categories(cfg), [("unknown_key", "")])
        self.assertEqual(cfg.values, {"state_dir": "/keep"})

    def test_env_only_things_are_unknown(self):
        cfg = self.load("llm:\n  api_key: x\n  openai_api_key: y\n")
        self.assertEqual(self.categories(cfg), [("unknown_key", "llm"), ("unknown_key", "llm")])

    def test_bad_type_cases(self):
        cases = {
            "inject_dispositions: 1\n": "inject_dispositions",
            "inject_dispositions: yes_please\n": "inject_dispositions",
            "state_dir: 5\n": "state_dir",
            "llm: foo\n": "llm",
            "llm:\n  model: no\n": "llm.model",          # YAML 1.1: no -> False
            "llm:\n  backend: 3\n": "llm.backend",
            "llm:\n  timeout_sec: '5'\n": "llm.timeout_sec",
            "llm:\n  timeout_sec: true\n": "llm.timeout_sec",       # bool は数として受けない
            "llm:\n  openai_max_completion_tokens: true\n": "llm.openai_max_completion_tokens",
            "llm:\n  openai_max_completion_tokens: 1.5\n": "llm.openai_max_completion_tokens",
        }
        for text, key in cases.items():
            with self.subTest(text=text):
                cfg = self.load(text)
                self.assertEqual(self.categories(cfg), [("bad_type", key)])
                self.assertFalse(cfg.whole_file)
                self.assertEqual(cfg.values, {})

    def test_bad_value_cases(self):
        cases = {
            "state_dir: relative/dir\n": "state_dir",
            "state_dir: ''\n": "state_dir",
            "llm:\n  backend: gpt\n": "llm.backend",
            "llm:\n  model: ''\n": "llm.model",
            "llm:\n  timeout_sec: 0\n": "llm.timeout_sec",
            "llm:\n  timeout_sec: -3\n": "llm.timeout_sec",
            "llm:\n  timeout_sec: .inf\n": "llm.timeout_sec",
            "llm:\n  openai_max_completion_tokens: 0\n": "llm.openai_max_completion_tokens",
        }
        for text, key in cases.items():
            with self.subTest(text=text):
                cfg = self.load(text)
                self.assertEqual(self.categories(cfg), [("bad_value", key)])
                self.assertEqual(cfg.values, {})

    def test_int_timeout_accepted(self):
        self.assertEqual(self.load("llm:\n  timeout_sec: 30\n").values, {"llm.timeout_sec": 30})


class TestHostileValues(ConfigCase):
    def test_load_never_raises_on_hostile_values(self):
        cfg = self.load(
            'state_dir: "/a\\0b"\ninject_dispositions: true\n'
            "llm:\n  timeout_sec: {}\n  model: m\n  backend: [x]\n  command: {{a: 1}}\n".format("9" * 400)
        )
        self.assertFalse(cfg.whole_file)
        self.assertEqual(cfg.values, {"inject_dispositions": True, "llm.model": "m"})
        self.assertEqual({(i.category, i.key) for i in cfg.issues}, {
            ("bad_value", "state_dir"), ("bad_value", "llm.timeout_sec"),
            ("bad_type", "llm.backend"), ("bad_type", "llm.command")})
        for text in ("1.0e+400", ".nan", "-" + "9" * 400, "9" * 400):
            with self.subTest(text=text):
                c = self.load("llm:\n  timeout_sec: {}\n  model: m\n".format(text))
                self.assertEqual(c.values, {"llm.model": "m"})
                self.assertEqual(self.categories(c), [("bad_value", "llm.timeout_sec")])

    def test_hostile_ints_are_total_and_bounded(self):
        hexbig = "0x" + "f" * 5000
        sexa = ":".join(["59"] * 3000)
        for text in ("version: {}\n".format(hexbig), "version: {}\n".format(sexa)):
            with self.subTest(text=text[:20]):
                c = self.load(text)
                self.assertEqual(self.categories(c), [("bad_version", "")])
                self.assertTrue(c.whole_file)
                self.assertLess(len(config.describe_issues(c)), 600)
        c = self.load("state_dir: /keep\nllm:\n  timeout_sec: {}\n  openai_max_completion_tokens: -{}\n  model: m\n"
                      .format(hexbig, hexbig))
        self.assertFalse(c.whole_file)
        self.assertEqual(c.values, {"state_dir": "/keep", "llm.model": "m"})
        self.assertEqual(sorted(self.categories(c)), [
            ("bad_value", "llm.openai_max_completion_tokens"), ("bad_value", "llm.timeout_sec")])
        self.assertLess(len(config.describe_issues(c)), 600)
        c = self.load("{}: 1\nstate_dir: /keep\n".format("K" * 900))
        self.assertEqual(self.categories(c), [("unknown_key", "")])
        self.assertLess(len(config.describe_issues(c)), 600)

    def test_fifo_and_oversized_file_are_unreadable_without_hanging(self):
        import threading
        fifo = os.path.join(self.root, "fifo.yaml")
        os.mkfifo(fifo)
        big = self.write("# " + "x" * (config.MAX_CONFIG_BYTES + 10) + "\n", os.path.join(self.root, "big.yaml"))
        results = {}

        def run():
            results["fifo"] = config.load(fifo)
            results["big"] = config.load(big)
            results["dir"] = config.load(self.root)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(10)
        self.assertFalse(t.is_alive(), "load() hung on a FIFO")
        for name in ("fifo", "big", "dir"):
            self.assertEqual(self.categories(results[name]), [("unreadable", "")], name)
            self.assertTrue(results[name].whole_file)

    def test_get_never_raises_even_if_load_does(self):
        with mock.patch.object(config, "load", side_effect=RuntimeError("boom")), \
                mock.patch.dict(os.environ, {"RAIYA_CONFIG": self.path}):
            cfg = config.get()
        self.assertTrue(cfg.whole_file)
        self.assertEqual(cfg.values, {})
        self.assertTrue(config.state_dir_lost(cfg))

    def test_load_total_when_internals_raise(self):
        self.write("state_dir: /x\n")
        with mock.patch.object(config, "_validate", side_effect=RuntimeError("boom")):
            self.assertTrue(config.load(self.path).whole_file)

    def test_validate_crash_becomes_whole_file_issue(self):
        self.write("state_dir: /x\n")
        with mock.patch.object(config, "_validate", side_effect=RuntimeError("boom")):
            cfg = config.load(self.path)
        self.assertTrue(cfg.whole_file)
        self.assertEqual(cfg.values, {})
        self.assertEqual([i.category for i in cfg.issues], ["bad_value"])
        self.assertLess(len(config.summary(cfg)), 100)


class TestNoLeak(ConfigCase):
    def test_summary_and_keys_have_no_user_text(self):
        cfg = self.load(
            "SECRET_KEY_NAME: 1\nstate_dir: relative-secret-path\nllm:\n  SECRET_LLM_KEY: 1\n"
            "  model: no\n  backend: SECRET_BACKEND\n"
        )
        self.assertTrue(cfg.issues)
        text = config.summary(cfg) + repr([(i.category, i.key, i.line) for i in cfg.issues])
        for secret in ("SECRET", "relative-secret-path", self.root):
            self.assertNotIn(secret, text)
        # detail には載る (stderr 用)
        self.assertIn("SECRET_KEY_NAME", " ".join(i.detail for i in cfg.issues))

    def test_summary_format_and_bound(self):
        cfg = self.load("llm:\n  model: no\n")
        self.assertEqual(config.summary(cfg), "category=bad_type key=llm.model")
        many = config.Config(None, {}, tuple(config.Issue("bad_type", "llm.model", 3, "") for _ in range(30)),
                             False)
        line = config.summary(many)
        self.assertIn("line=3", line)
        self.assertLess(len(line), 400)
        self.assertNotIn("\n", line)

    def test_state_dir_lost(self):
        self.assertTrue(config.state_dir_lost(self.load("- x\n")))
        self.assertTrue(config.state_dir_lost(self.load("state_dir: rel\n")))
        self.assertFalse(config.state_dir_lost(self.load("llm:\n  model: no\n")))
        self.assertFalse(config.state_dir_lost(config.load(None)))
        self.assertFalse(config.state_dir_lost(self.load("state_dir: /ok\n")))


class TestMemo(ConfigCase):
    def test_read_once_per_path_and_reset(self):
        self.write("inject_dispositions: true\n")
        with mock.patch.dict(os.environ, {"RAIYA_CONFIG": self.path}):
            first = config.get()
            self.write("inject_dispositions: false\n")
            self.assertIs(config.get(), first)  # 書き換えても同じプロセスでは読み直さない
            self.assertEqual(config.get().values, {"inject_dispositions": True})
            config._reset()
            self.assertEqual(config.get().values, {"inject_dispositions": False})

    def test_path_change_rereads_without_reset(self):
        other = os.path.join(self.root, "other.yaml")
        self.write("inject_dispositions: true\n")
        self.write("inject_dispositions: false\n", other)
        with mock.patch.dict(os.environ, {"RAIYA_CONFIG": self.path}):
            self.assertEqual(config.get().values, {"inject_dispositions": True})
        with mock.patch.dict(os.environ, {"RAIYA_CONFIG": other}):
            self.assertEqual(config.get().values, {"inject_dispositions": False})

    def test_no_file_does_not_import_yaml(self):
        code = (
            "import sys, os\nos.environ['RAIYA_CONFIG']=''\n"
            "from raiya import config\nconfig.get()\nprint('yaml' in sys.modules)\n"
        )
        import subprocess
        out = subprocess.run([sys.executable, "-I", "-c", "import sys; sys.path.insert(0, %r)\n%s" % (REPO_ROOT, code)],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.stdout.strip(), "False", out.stderr)


class TestAstPin(unittest.TestCase):
    def test_config_imports_no_raiya_internals(self):
        with open(os.path.join(REPO_ROOT, "raiya", "config.py"), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        names = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names.append(("." * node.level) + (node.module or ""))
                names += [a.name for a in node.names]
        for forbidden in ("backend", "engine", "hooks", "raiya"):
            for n in names:
                self.assertNotIn(forbidden, n.replace(".", " ").split(), n)
        self.assertFalse([n for n in names if n.startswith(".") or n.startswith("raiya")], names)


if __name__ == "__main__":
    unittest.main()
