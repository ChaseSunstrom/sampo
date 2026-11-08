#!/usr/bin/env python3
# T-2288: fm prove --migration: up, down, up again on a throwaway sqlite file, schema compared each time
import json
import os
import subprocess
import sys
import tempfile
from unittest import mock

from helpers import ForemanTestCase

from sampo import prove as sprove
from sampo import infra as fmprove_infra

M = "migrations/"
GOOD = {M + "001_users.up.sql": "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);",
        M + "001_users.down.sql": "DROP TABLE users;",
        M + "002_idx.up.sql": "CREATE TABLE logs (users_name TEXT);",
        M + "002_idx.down.sql": "DROP TABLE logs;"}


def write(root, files):
    for rel, text in files.items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)


class Migration(ForemanTestCase):
    def proj(self, files, name="proj"):
        root = os.path.join(self.tmp, name)
        write(root, files)
        return root

    def test_a_reversible_pair_passes_with_the_schema_before_and_after(self):
        r = sprove.migration(self.proj(GOOD), True)
        self.assertEqual(r["verdict"], "pass", r)
        self.assertEqual(r["tool"], "sql")
        self.assertEqual(r["before"], [])
        self.assertEqual(r["after_up"], r["after_up2"])
        self.assertIn("CREATE TABLE users", "".join(r["after_up"]))

    def test_a_down_that_leaves_something_behind_fails_and_says_what(self):
        files = dict(GOOD, **{M + "002_idx.down.sql": "SELECT 1;"})
        r = sprove.migration(self.proj(files), True)
        self.assertEqual(r["verdict"], "fail")
        self.assertIn("down", r["why"])
        self.assertIn("logs", r["why"])

    def test_a_missing_down_and_a_broken_up_fail(self):
        r = sprove.migration(self.proj({M + "001_a.up.sql": "CREATE TABLE a (x);"}), True)
        self.assertEqual(r["verdict"], "fail")
        self.assertIn("001_a", r["why"])
        r = sprove.migration(self.proj({M + "001_a.up.sql": "INSERT INTO nope VALUES (1);", M + "001_a.down.sql": "SELECT 1;"}, "p2"), True)
        self.assertEqual(r["verdict"], "fail")
        self.assertIn("up", r["why"])

    def test_untrusted_unknown_and_unsupported_tools_run_nothing(self):
        r = sprove.migration(self.proj(GOOD), False)
        self.assertEqual(r["verdict"], "refused")
        self.assertIn("--yes-run", r["why"])
        r = sprove.migration(self.proj({"app.py": "x = 1"}, "p3"), True)
        self.assertEqual(r["verdict"], "unknown")
        with mock.patch.dict(os.environ, {"PATH": os.path.join(self.tmp, "nobin")}):  # T-2386: a named tool is unproven, not unknown
            r = sprove.migration(self.proj({"alembic.ini": "[alembic]"}, "p4"), True)
        self.assertEqual(r["verdict"], "unproven")
        self.assertIn("alembic", r["why"])

    def test_sql_cannot_reach_a_file_outside_the_throwaway_database(self):
        victim = os.path.join(self.tmp, "real.db")
        files = {M + "001_a.up.sql": f"ATTACH DATABASE '{victim}' AS x; CREATE TABLE x.t (a);",
                 M + "001_a.down.sql": "SELECT 1;"}
        r = sprove.migration(self.proj(files), True)
        self.assertEqual(r["verdict"], "fail")
        self.assertFalse(os.path.exists(victim))

    def test_a_runaway_migration_is_cut_off(self):
        loop = "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n) SELECT count(*) FROM n;"
        r = sprove.migration(self.proj({M + "001_a.up.sql": loop, M + "001_a.down.sql": "SELECT 1;"}), True, limit=1)
        self.assertEqual(r["verdict"], "fail")
        self.assertIn("time", r["why"])

    def test_the_command_refuses_an_untrusted_tree_and_passes_a_trusted_one(self):
        root = self.proj(GOOD)
        r = self.fm("prove", "--migration", root, check=False)
        self.assertEqual(r.returncode, 2, r.stderr)
        r = self.fm("prove", "--migration", root, "--yes-run", "--json", check=False)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["verdict"], "pass")


DBMATE = "-- migrate:up\nCREATE TABLE users (id INTEGER PRIMARY KEY);\n\n-- migrate:down\nDROP TABLE users;\n"
NO_PG = "/nonexistent/fm-no-postgres/*/bin"  # T-2439: FOREMAN_PG_DIRS to a place no Postgres is
PG_SQL = {M + "001_a.up.sql": "CREATE TYPE mood AS ENUM ('a');\nCREATE TABLE accounts (id uuid);",
          M + "001_a.down.sql": "DROP TABLE accounts;\nDROP TYPE mood;"}  # T-2439: a real Postgres keeps the type otherwise
ALEMBIC_INI = ("[alembic]\nscript_location = %(here)s/alembic\nsqlalchemy.url = {url}\n\n[loggers]\nkeys = root\n")
ENV_PY = "from alembic import context\nurl = context.config.get_main_option('sqlalchemy.url')\n"
ALEMBIC = """
import configparser, json, os, sqlite3, sys
a = sys.argv[1:]
cp = configparser.ConfigParser(interpolation=None)
cp.read(a[a.index("-c") + 1])
url = cp["alembic"]["sqlalchemy.url"]
with open(@LOG@, "a") as f:
    f.write(json.dumps({"argv": a, "url": url, "dotenv": os.path.exists(".env"),
                        "env": sorted(k for k in os.environ if k.startswith(("DATABASE", "PG", "MYSQL")))}) + "\\n")
if os.path.exists(@BREAK@):
    sys.exit(open(@BREAK@).read() or "sqlalchemy.exc.ProgrammingError: boom")
import types
cfg = types.SimpleNamespace(get_main_option=lambda k: cp["alembic"][k], set_main_option=lambda k, v: cp["alembic"].__setitem__(k, v))
ctx = types.ModuleType("alembic.context")
ctx.config = cfg
pkg = types.ModuleType("alembic")
pkg.context = ctx
sys.modules.update({"alembic": pkg, "alembic.context": ctx})
envpy = os.path.join(cp["alembic"]["script_location"].replace("%(here)s", os.getcwd()), "env.py")
if os.path.exists(envpy):  # the tree's own env.py runs, as the real tool would run it
    sys.path.insert(0, os.path.dirname(envpy))
    exec(compile(open(envpy).read(), envpy, "exec"), {"__file__": envpy})
db = sqlite3.connect(url.split("sqlite:///", 1)[1])
if a[-2:] == ["upgrade", "head"]:
    db.execute("CREATE TABLE IF NOT EXISTS alembic_version (v)")
    db.execute("CREATE TABLE IF NOT EXISTS widgets (id)")
elif a[-2:] == ["downgrade", "base"] and not os.path.exists(@LEAK@):
    db.execute("DROP TABLE IF EXISTS widgets")
db.commit()
"""
MANAGE = """
import json, os, sqlite3, sys
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "proj.settings")
import django
django.setup()
from django.conf import settings
name = settings.DATABASES["default"]["NAME"]
a = sys.argv[1:]
with open(@LOG@, "a") as f:
    f.write(json.dumps({"argv": a[:2] if a[:1] == ["shell"] else a, "name": name, "settings": os.environ["DJANGO_SETTINGS_MODULE"],
                        "env": sorted(k for k in os.environ if k.startswith(("DATABASE", "PG", "MYSQL")))}) + "\\n")
if a[:2] == ["shell", "-c"]:  # manage.py shell -c: the code runs with the settings manage.py really set up
    exec(a[2])
    sys.exit(0)
if os.path.exists(@BREAK@):
    sys.exit("django.db.utils.OperationalError: boom")
db = sqlite3.connect(name)
if a == ["migrate"]:
    db.execute("CREATE TABLE IF NOT EXISTS django_migrations (id)")
    db.execute("CREATE TABLE IF NOT EXISTS shop_item (id)")
elif a[-1:] == ["zero"] and not os.path.exists(@LEAK@):
    db.execute("DROP TABLE IF EXISTS shop_item")
db.commit()
"""
REAL_PATH = os.environ.get("PATH", "")  # before any test pins it to stubs


class ToolCase(ForemanTestCase):
    """T-2386: stubs on PATH stand in for the repo's own migration tools (alembic, Django, psql); DB variables are set."""

    def setUp(self):
        super().setUp()
        self.bin, self.log = os.path.join(self.tmp, "bin"), os.path.join(self.tmp, "calls.log")
        self.leak, self.brk = os.path.join(self.tmp, "leak"), os.path.join(self.tmp, "break")
        os.makedirs(self.bin)
        patch = mock.patch.dict(os.environ, {"PATH": self.bin, "DATABASE_URL": "postgres://real/prod", "PGHOST": "real",
                                             "MYSQL_HOST": "real", "FOREMAN_PG_DIRS": NO_PG})  # T-2439: no runner's own
        patch.start()
        self.addCleanup(patch.stop)

    def stub(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, "w") as f:
            f.write(f"#!{sys.executable}\n" + body.replace("@LOG@", repr(self.log)).replace("@LEAK@", repr(self.leak))
                    .replace("@BREAK@", repr(self.brk)))
        os.chmod(path, 0o755)

    def python3(self):
        """A python3 whose Django is a tiny fake: its settings are the module DJANGO_SETTINGS_MODULE names."""
        fake = os.path.join(self.tmp, "fake")
        write(fake, {"django/__init__.py": "def setup():\n    import importlib, os\n    from django import conf\n"
                     "    conf.settings = importlib.import_module(os.environ['DJANGO_SETTINGS_MODULE'])\n",
                     "django/conf.py": "settings = None\n"})
        path = os.path.join(self.bin, "python3")
        with open(path, "w") as f:
            f.write(f'#!/bin/sh\nPYTHONPATH={fake}${{PYTHONPATH:+:$PYTHONPATH}} exec {sys.executable} "$@"\n')
        os.chmod(path, 0o755)

    def calls(self):
        try:
            with open(self.log) as f:
                return [json.loads(x) for x in f.read().splitlines()]
        except OSError:
            return []

    def alembic_tree(self, url="sqlite:///dev.db", name="al", env_py=ENV_PY):
        self.stub("alembic", ALEMBIC)
        return self.proj_at(name, {"alembic.ini": ALEMBIC_INI.format(url=url), "alembic/env.py": env_py,
                                   "alembic/versions/001_widgets.py": "# revision\n"})

    def django_tree(self, name="dj"):
        self.python3()
        root = self.proj_at(name, {"proj/__init__.py": "", "proj/settings.py":
                                   "DATABASES = {'default': {'ENGINE': 'django.db.backends.postgresql', 'NAME': 'prod'}}\n",
                                   "shop/migrations/0001_initial.py": "# migration\n"})
        write(root, {"manage.py": MANAGE.replace("@LOG@", repr(self.log)).replace("@LEAK@", repr(self.leak))
                     .replace("@BREAK@", repr(self.brk))})
        return root

    def proj_at(self, name, files):
        root = os.path.join(self.tmp, name)
        write(root, files)
        return root


class MigrationTools(ToolCase):
    """T-2386: the repo's own migration tool on a throwaway database."""

    def test_migration_tool_a_dbmate_file_runs_its_up_and_down_sections_and_passes(self):
        r = sprove.migration(self.proj_at("db", {"db/migrations/20240101_users.sql": DBMATE}), True)
        self.assertEqual((r["verdict"], r["tool"]), ("pass", "dbmate"), r)
        self.assertIn("CREATE TABLE users", "".join(r["after_up"]))
        self.assertEqual(r["after_up"], r["after_up2"])
        self.assertEqual(r["after_down"], r["before"])

    def test_migration_tool_a_dbmate_down_that_leaves_a_table_fails_and_a_file_without_down_fails(self):
        left = DBMATE.replace("DROP TABLE users;", "SELECT 1;")
        r = sprove.migration(self.proj_at("db", {"db/migrations/1_users.sql": left}), True)
        self.assertEqual(r["verdict"], "fail", r)
        self.assertIn("users", r["why"])
        r = sprove.migration(self.proj_at("db2", {"db/migrations/1_users.sql": "-- migrate:up\nCREATE TABLE t (a);\n"}), True)
        self.assertEqual(r["verdict"], "fail", r)
        self.assertIn("down", r["why"])

    def test_migration_tool_alembic_runs_upgrade_downgrade_upgrade_on_a_generated_config(self):
        r = sprove.migration(self.alembic_tree(), True)
        self.assertEqual((r["verdict"], r["tool"]), ("pass", "alembic"), r)
        steps = [x["argv"][-2:] for x in self.calls()]
        self.assertEqual(steps, [["upgrade", "head"], ["downgrade", "base"], ["upgrade", "head"]])
        self.assertEqual(r["after_up"], r["after_up2"])
        self.assertEqual(r["after_down"], r["before"])
        self.assertIn("widgets", "".join(r["after_up"]))
        self.assertNotIn("alembic_version", "".join(r["after_up"]))  # the tool's own bookkeeping is not the schema

    def test_migration_tool_alembic_a_downgrade_that_leaves_a_table_fails(self):
        open(self.leak, "w").close()
        r = sprove.migration(self.alembic_tree(), True)
        self.assertEqual(r["verdict"], "fail", r)
        self.assertIn("downgrade", r["why"])
        self.assertIn("widgets", r["why"])

    def test_migration_tool_alembic_not_installed_says_what_it_needs_and_untrusted_runs_nothing(self):
        root = self.alembic_tree()
        os.remove(os.path.join(self.bin, "alembic"))
        r = sprove.migration(root, True)
        self.assertEqual(r["verdict"], "unproven", r)
        self.assertIn("alembic", r["why"])
        self.assertIn("PATH", r["why"])
        self.stub("alembic", ALEMBIC)
        self.assertEqual(sprove.migration(root, False)["verdict"], "refused")
        self.assertEqual(self.calls(), [])

    def test_migration_tool_django_runs_migrate_app_zero_migrate_on_a_generated_settings_module(self):
        r = sprove.migration(self.django_tree(), True)
        self.assertEqual((r["verdict"], r["tool"]), ("pass", "django"), r)
        self.assertEqual([x["argv"] for x in self.calls()],  # the first is the settings probe, through manage.py itself
                         [["shell", "-c"], ["migrate"], ["migrate", "shop", "zero"], ["migrate"]])
        self.assertEqual({x["settings"] for x in self.calls()}, {"fm_prove_settings"})
        self.assertEqual(r["after_up"], r["after_up2"])
        self.assertIn("shop_item", "".join(r["after_up"]))
        self.assertNotIn("shop_item", "".join(r["after_down"]))

    def test_migration_tool_django_a_zero_that_leaves_the_apps_table_fails(self):
        open(self.leak, "w").close()
        r = sprove.migration(self.django_tree(), True)
        self.assertEqual(r["verdict"], "fail", r)
        self.assertIn("shop_item", r["why"])

    def test_migration_tool_prisma_and_rails_say_what_they_need_instead_of_unknown(self):
        r = sprove.migration(self.proj_at("pr", {"prisma/schema.prisma": "model A {}\n",
                                                  "prisma/migrations/1/migration.sql": "SELECT 1;"}), True)
        self.assertEqual((r["verdict"], r["tool"]), ("unproven", "prisma"), r)
        self.assertIn("prisma", r["why"])
        self.assertIn("DATABASE_URL", r["why"])
        r = sprove.migration(self.proj_at("rb", {"Gemfile": "gem 'rails'\n", "db/migrate/20240101_a.rb": "# m\n",
                                                  "config/database.yml": "default: {}\n"}), True)
        self.assertEqual((r["verdict"], r["tool"]), ("unproven", "rails"), r)
        self.assertIn("rails db:rollback", r["why"])

    def test_migration_tool_the_command_exits_two_for_unproven(self):
        r = self.fm("prove", "--migration", self.proj_at("pr", {"prisma/schema.prisma": "model A {}\n"}), "--yes-run",
                    check=False)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("unproven", r.stdout)


class MigrationIsolation(ToolCase):
    """T-2386: the real database is never reached."""

    def test_migration_isolation_the_tool_gets_no_connection_variables_and_no_dotenv(self):
        root = self.alembic_tree()
        write(root, {".env": "DATABASE_URL=postgres://real/prod\n"})
        r = sprove.migration(root, True)
        self.assertEqual(r["verdict"], "pass", r)
        self.assertTrue(self.calls())
        for x in self.calls():
            self.assertEqual(x["env"], [], x)
            self.assertFalse(x["dotenv"])
        r = sprove.migration(self.django_tree(), True)
        self.assertEqual(r["verdict"], "pass", r)
        self.assertEqual([x["env"] for x in self.calls() if "name" in x], [[], [], [], []])

    def test_migration_isolation_alembic_is_pointed_at_the_throwaway_database_by_its_own_url_setting(self):
        root = self.alembic_tree(url="postgresql://prod.example/real")
        before = open(os.path.join(root, "alembic.ini")).read()
        self.assertEqual(sprove.migration(root, True)["verdict"], "pass")
        for x in self.calls():
            self.assertTrue(x["url"].startswith("sqlite:///" + tempfile.gettempdir()), x)
            self.assertIn("fm-prove-", x["url"])
        self.assertEqual(open(os.path.join(root, "alembic.ini")).read(), before)  # the tree's own file is untouched

    def test_migration_isolation_alembic_whose_url_is_not_the_throwaway_one_is_refused_before_any_run(self):
        with mock.patch("sampo.migrate._alembic_ini", lambda text, url: text):
            r = sprove.migration(self.alembic_tree(url="postgresql://prod.example/real"), True)
        self.assertEqual(r["verdict"], "refused", r)
        self.assertIn("throwaway", r["why"])
        self.assertEqual(self.calls(), [])

    def test_migration_isolation_an_env_py_that_names_its_own_database_is_refused_before_any_run(self):
        root = self.alembic_tree(env_py="from sqlalchemy import create_engine\ne = create_engine('postgresql://prod/real')\n")
        r = sprove.migration(root, True)
        self.assertEqual(r["verdict"], "refused", r)
        self.assertIn("env.py", r["why"])
        self.assertEqual(self.calls(), [])

    def test_migration_isolation_django_settings_are_replaced_and_a_database_that_resolves_elsewhere_is_refused(self):
        r = sprove.migration(self.django_tree(), True)
        self.assertEqual(r["verdict"], "pass", r)
        self.assertTrue(all(x["name"].startswith(tempfile.gettempdir()) and "fm-prove-" in x["name"]
                            for x in self.calls()), self.calls())
        os.remove(self.log)
        with mock.patch("sampo.migrate._django_settings", lambda mod, db: f"from {mod} import *\n"):
            r = sprove.migration(self.django_tree(), True)
        self.assertEqual(r["verdict"], "refused", r)
        self.assertIn("throwaway", r["why"])
        self.assertEqual([x for x in self.calls() if x["argv"][0] == "migrate"], [])  # only the probe ran

    def test_migration_isolation_a_failing_tool_run_never_names_the_throwaway_folder_and_leaves_nothing(self):
        open(self.brk, "w").close()
        held = {n for n in os.listdir(tempfile.gettempdir()) if n.startswith("fm-prove-")}
        r = sprove.migration(self.alembic_tree(), True)
        self.assertEqual(r["verdict"], "fail", r)
        self.assertNotIn(tempfile.gettempdir() + "/fm-prove-", r["why"])
        self.assertEqual({n for n in os.listdir(tempfile.gettempdir()) if n.startswith("fm-prove-")} - held, set())


STUB_PG = {
    "initdb": "import os, sys\nopen(@LOG@, 'a').write(json.dumps({'tool': 'initdb', 'argv': sys.argv[1:]}) + '\\n')\n"
              "os.makedirs(sys.argv[sys.argv.index('-D') + 1])\n",
    "pg_ctl": "import os, sys\na = sys.argv[1:]\nopen(@LOG@, 'a').write(json.dumps({'tool': 'pg_ctl', 'argv': a}) + '\\n')\n"
              "if 'start' in a:\n    o = a[a.index('-o') + 1]\n    assert \"listen_addresses=''\" in o, o\n"
              "    os.makedirs(o.split('unix_socket_directories=')[1].split()[0], exist_ok=True)\n",
    "psql": "import os, re, sys\na = sys.argv[1:]\nsock = a[a.index('-h') + 1]\nstate = os.path.join(sock, 'tables')\n"
            "open(@LOG@, 'a').write(json.dumps({'tool': 'psql', 'sock': sock, "
            "'argv': [x[:40] for x in a]}) + '\\n')\n"
            "tables = set(open(state).read().split()) if os.path.exists(state) else set()\n"
            "if '-c' in a:\n    print(chr(10).join('relation r ' + t for t in sorted(tables)))\n    sys.exit(0)\n"
            "sql = open(a[a.index('-f') + 1]).read()\n"
            "if 'BAD' in sql:\n    sys.exit('psql: ERROR: syntax error at or near BAD')\n"
            "tables |= set(re.findall(r'CREATE TABLE (\\w+)', sql))\ntables -= set(re.findall(r'DROP TABLE (\\w+)', sql))\n"
            "open(state, 'w').write(' '.join(sorted(tables)))\n",
}


class MigrationDialect(ToolCase):
    """T-2386: SQL for Postgres runs on a throwaway Postgres cluster when initdb is installed, else it's unproven."""

    def test_the_schema_query_casts_char_and_name_columns_before_concatenating(self):  # T-2438
        # newer Postgres (Ubuntu 26 runners) rejects "char" || unknown as ambiguous; text || text is never ambiguous
        from sampo import migrate
        for col in ("c.relkind", "c.relname", "a.attname"):
            with self.subTest(col=col):
                self.assertIn(f"{col}::text ||", migrate.PG_SCHEMA)

    def pg_stubs(self):
        for name, body in STUB_PG.items():
            self.stub(name, "import json\n" + body)

    def test_migration_dialect_postgres_only_sql_without_initdb_is_unproven_not_failed(self):
        r = sprove.migration(self.proj_at("pg", PG_SQL), True)
        self.assertEqual(r["verdict"], "unproven", r)
        self.assertIn("dialect", r["why"])
        self.assertIn("initdb", r["why"])
        self.assertEqual(r["dialect"], "postgres")
        c = self.fm("prove", "--migration", self.proj_at("pg", PG_SQL), "--yes-run", check=False)
        self.assertEqual(c.returncode, 2, c.stdout + c.stderr)  # exit 1 stays for a real failure

    def test_migration_dialect_a_dbmate_file_for_postgres_is_unproven_too_and_plain_errors_still_fail(self):
        pg = "-- migrate:up\nCREATE TYPE mood AS ENUM ('a');\n-- migrate:down\nDROP TYPE mood;\n"
        r = sprove.migration(self.proj_at("pg", {"db/migrations/1_m.sql": pg}), True)
        self.assertEqual(r["verdict"], "unproven", r)
        r = sprove.migration(self.proj_at("bad", {"db/migrations/1_m.sql": "-- migrate:up\nINSERT INTO nope VALUES (1);\n"
                                                   "-- migrate:down\n"}), True)
        self.assertEqual(r["verdict"], "fail", r)  # no such table is the migration's fault, not the dialect's

    def test_migration_dialect_postgres_only_sql_runs_on_a_socket_only_cluster_in_a_temp_dir_removed_after(self):
        self.pg_stubs()
        r = sprove.migration(self.proj_at("pg", PG_SQL), True)
        self.assertEqual((r["verdict"], r["engine"]), ("pass", "postgres"), r)
        calls = self.calls()
        tools = [x["tool"] for x in calls]
        self.assertEqual(tools[0], "initdb")
        self.assertTrue(tools.count("psql") >= 3)
        start = next(x for x in calls if x["tool"] == "pg_ctl" and "start" in x["argv"])
        self.assertIn("-w", start["argv"])
        self.assertEqual(calls[-1]["argv"][-1], "stop")
        psql = next(x for x in calls if x["tool"] == "psql")
        self.assertTrue(psql["sock"].startswith("/tmp/fm-pg-") and len(psql["sock"]) < 60, psql["sock"])  # sun_path is 107 bytes
        self.assertFalse(os.path.exists(psql["sock"]))  # the socket folder is gone

    def test_migration_dialect_a_statement_postgres_rejects_fails_and_portable_sql_never_starts_a_cluster(self):
        self.pg_stubs()
        bad = dict(PG_SQL, **{M + "001_a.up.sql": "CREATE TABLE a (id uuid); BAD;"})
        r = sprove.migration(self.proj_at("pg", bad), True)
        self.assertEqual((r["verdict"], r["engine"]), ("fail", "postgres"), r)
        self.assertIn("BAD", r["why"])
        os.remove(self.log)
        r = sprove.migration(self.proj_at("ok", GOOD), True)
        self.assertEqual((r["verdict"], r["engine"]), ("pass", "sqlite"), r)
        self.assertEqual(self.calls(), [])

    def test_migration_dialect_a_tool_that_fails_on_sqlite_in_a_postgres_tree_is_unproven_only_for_a_dialect_error(self):
        with open(self.brk, "w") as f:
            f.write('sqlalchemy.exc.ProgrammingError: (sqlite3.OperationalError) near "SERIAL": syntax error')
        r = sprove.migration(self.alembic_tree(url="postgresql://prod.example/real"), True)
        self.assertEqual(r["verdict"], "unproven", r)
        self.assertEqual(r["dialect"], "postgres")
        r = sprove.migration(self.alembic_tree(url="sqlite:///dev.db", name="lite"), True)
        self.assertEqual(r["verdict"], "fail", r)  # a dialect error in a sqlite project is the migration's own

    def test_migration_dialect_a_broken_downgrade_in_a_postgres_tree_is_a_failure_not_unproven(self):
        with open(self.brk, "w") as f:
            f.write("sqlalchemy.exc.NoSuchTableError: widgets")
        r = sprove.migration(self.alembic_tree(url="postgresql://prod.example/real"), True)
        self.assertEqual(r["verdict"], "fail", r)
        self.assertIn("widgets", r["why"])
        self.assertNotIn("dialect", r)

    def test_migration_dialect_a_missing_module_is_the_environment_not_a_failed_migration(self):
        self.stub("alembic", "import sys\nsys.exit(\"ModuleNotFoundError: No module named 'app'\")\n")
        r = sprove.migration(self.proj_at("al", {"alembic.ini": ALEMBIC_INI.format(url="sqlite:///x.db"),
                                                  "alembic/env.py": ENV_PY}), True)
        self.assertEqual(r["verdict"], "unproven", r)
        self.assertIn("No module", r["why"])

    def test_migration_dialect_sql_that_can_reach_outside_the_throwaway_cluster_is_unproven_and_never_starts_one(self):
        self.pg_stubs()
        danger = {"a psql meta-command": "CREATE TABLE t (a uuid);\n\\! id\n",
                  "a meta-command after SQL": "CREATE TABLE t (a uuid); \\copy t to /tmp/x\n",
                  "COPY PROGRAM": "CREATE TABLE t (a uuid); COPY t FROM PROGRAM 'id';",
                  "COPY to a file": "CREATE TABLE t (a uuid); COPY t TO '/tmp/x';",
                  "COPY from a file": "CREATE TABLE t (a uuid); COPY t FROM '/etc/passwd';",
                  "dblink": "CREATE TABLE t (a uuid); SELECT dblink('host=prod', 'select 1');",
                  "a foreign data wrapper": "CREATE TABLE t (a uuid); CREATE SERVER s FOREIGN DATA WRAPPER postgres_fdw;",
                  "pg_read_file": "CREATE TABLE t (a uuid); SELECT pg_read_file('/etc/passwd');",
                  "pg_read_binary_file": "CREATE TABLE t (a uuid); SELECT pg_read_binary_file('/etc/passwd');",
                  "lo_import": "CREATE TABLE t (a uuid); SELECT lo_import('/etc/passwd');",
                  "lo_export": "CREATE TABLE t (a uuid); SELECT lo_export(1, '/tmp/x');",
                  "CREATE EXTENSION": "CREATE EXTENSION IF NOT EXISTS pgcrypto;"}
        for i, (what, sql) in enumerate(danger.items()):
            with self.subTest(what):
                files = {M + "001_a.up.sql": sql, M + "001_a.down.sql": "DROP TABLE t;"}
                r = sprove.migration(self.proj_at(f"d{i}", files), True)
                self.assertEqual(r["verdict"], "unproven", r)
                self.assertIn("unproven", r["why"] + r["verdict"])
                self.assertTrue(any(w in r["why"].lower() for w in ("meta-command", "copy", "dblink", "foreign", "pg_read",
                                                                    "lo_", "extension")), r["why"])
        self.assertEqual(self.calls(), [])  # no cluster started, no psql run

    def test_migration_dialect_more_postgres_only_syntax_picks_postgres_and_a_sqlite_syntax_error_retries_there(self):
        from sampo import migrate
        for sql in ("ALTER TABLE t ADD CONSTRAINT c CHECK (a > 0);", "ALTER TABLE t ALTER COLUMN a SET NOT NULL;",
                    "ALTER TABLE t ALTER COLUMN a TYPE bigint;", "DROP TABLE t CASCADE;",
                    "ALTER TABLE t ADD COLUMN IF NOT EXISTS b int;"):
            with self.subTest(sql):
                self.assertTrue(migrate.PG_ONLY.search(sql), sql)
        odd = {M + "001_a.up.sql": "CREATE TABLE t (a int);\nALTER TABLE t RENAME CONSTRAINT a TO b;",  # not in PG_ONLY
               M + "001_a.down.sql": "DROP TABLE t;"}
        r = sprove.migration(self.proj_at("nopg", odd), True)  # no Postgres here: the dialect's verdict, not a failure
        self.assertEqual(r["verdict"], "unproven", r)
        self.assertEqual(r["dialect"], "postgres")
        self.pg_stubs()
        r = sprove.migration(self.proj_at("withpg", odd), True)
        self.assertEqual((r["verdict"], r["engine"]), ("pass", "postgres"), r)

    def test_migration_dialect_foreman_pg_dirs_hides_the_postgres_folders(self):  # T-2439
        from sampo import migrate
        deb = os.path.join(self.tmp, "pgd", "16", "bin")
        os.makedirs(deb)
        self.assertEqual(migrate._pg_dirs(), [])  # ToolCase points it at nothing
        with mock.patch.dict(os.environ, {"FOREMAN_PG_DIRS": os.path.join(self.tmp, "pgd", "*", "bin")}):
            self.assertEqual(migrate._pg_dirs(), [deb])
        with mock.patch.dict(os.environ):
            os.environ.pop("FOREMAN_PG_DIRS")
            self.assertEqual(set(migrate._pg_dirs()), set(__import__("glob").glob(migrate.PG_DIRS)))

    def test_migration_dialect_a_real_postgres_cycles_the_fixture_where_one_is_installed(self):  # T-2439: CI has one
        from sampo import migrate
        with mock.patch.dict(os.environ, {"PATH": os.environ.get("FM_REAL_PATH") or REAL_PATH}):
            os.environ.pop("FOREMAN_PG_DIRS", None)
            bins, why = migrate._pg_bins(dict(os.environ), self.tmp)
            if not bins:
                self.skipTest(f"no real Postgres here: {why}")
            r = sprove.migration(self.proj_at("pg", PG_SQL), True)
        self.assertEqual((r["verdict"], r["engine"]), ("pass", "postgres"), r)

    def test_migration_dialect_postgres_binaries_under_usr_lib_postgresql_are_found(self):
        import shutil
        self.pg_stubs()
        deb = os.path.join(self.tmp, "pgd", "16", "bin")
        os.makedirs(deb)
        for n in STUB_PG:
            shutil.move(os.path.join(self.bin, n), os.path.join(deb, n))
        with mock.patch.dict(os.environ, {"FOREMAN_PG_DIRS": os.path.join(self.tmp, "pgd", "*", "bin")}):
            r = sprove.migration(self.proj_at("pg", PG_SQL), True)
        self.assertEqual((r["verdict"], r["engine"]), ("pass", "postgres"), r)

    def test_migration_dialect_a_migration_file_over_the_size_cap_is_unproven_not_failed(self):
        big = {M + "001_a.up.sql": "CREATE TABLE t (a);\n" + "-- " + "x" * (2 << 20) + "\n", M + "001_a.down.sql": "DROP TABLE t;"}
        r = sprove.migration(self.proj_at("big", big), True)
        self.assertEqual(r["verdict"], "unproven", r)
        self.assertIn("bytes", r["why"])

    def test_migration_dialect_migrations_run_in_version_order_not_text_order(self):
        files = {M + "2_a.up.sql": "CREATE TABLE a (x);", M + "2_a.down.sql": "DROP TABLE a;",
                 M + "10_b.up.sql": "ALTER TABLE a ADD COLUMN y;", M + "10_b.down.sql": "SELECT 1;"}
        r = sprove.migration(self.proj_at("nat", files), True)
        self.assertEqual(r["verdict"], "pass", r)
        self.assertEqual(r["migrations"], [M + "2_a", M + "10_b"])
        dbm = {"db/migrations/2_a.sql": "-- migrate:up\nCREATE TABLE a (x);\n-- migrate:down\nDROP TABLE a;\n",
               "db/migrations/10_b.sql": "-- migrate:up\nALTER TABLE a ADD COLUMN y;\n-- migrate:down\nSELECT 1;\n"}
        r = sprove.migration(self.proj_at("nat2", dbm), True)
        self.assertEqual(r["verdict"], "pass", r)

    def test_migration_dialect_vacuum_into_cannot_write_a_file_outside_the_throwaway_database(self):
        victim = os.path.join(self.tmp, "copy.db")
        files = {M + "001_a.up.sql": f"CREATE TABLE t (a); VACUUM INTO '{victim}';", M + "001_a.down.sql": "DROP TABLE t;"}
        r = sprove.migration(self.proj_at("vac", files), True)
        self.assertEqual(r["verdict"], "fail", r)
        self.assertFalse(os.path.exists(victim))


class MigrationGuard(ToolCase):
    """T-2386 review: repo code that opens a database by any road but the tool's own URL is stopped at the driver."""

    def drivers(self):
        """Fake psycopg2 and sqlalchemy that log every connect and engine; on the tool's path and on the fake Django's."""
        self.conn = os.path.join(self.tmp, "connects.log")
        mods = {"psycopg2.py": "import json\ndef connect(*a, **k):\n    open(%r, 'a').write(json.dumps([repr(x) for x in a]) + "
                               "'\\n')\n    return object()\n" % self.conn,
                "sqlalchemy/__init__.py": "import json\ndef create_engine(url, **k):\n    open(%r, 'a').write(json.dumps("
                                          "['engine', str(url)]) + '\\n')\n    return object()\n" % self.conn}
        write(self.bin, mods)
        write(os.path.join(self.tmp, "fake"), mods)

    def connects(self):
        try:
            with open(self.conn) as f:
                return f.read().splitlines()
        except OSError:
            return []

    def refused(self, r):
        self.assertEqual(r["verdict"], "refused", r)
        self.assertIn("throwaway", r["why"])
        self.assertEqual(self.connects(), [])

    def test_guard_an_env_py_taking_its_url_from_a_config_module_is_refused_and_nothing_connects(self):
        self.drivers()
        root = self.alembic_tree(env_py="from settings import DATABASE_URL\nimport psycopg2\npsycopg2.connect(DATABASE_URL)\n")
        write(root, {"alembic/settings.py": "DATABASE_URL = 'postgresql:///app'\n"})
        self.refused(sprove.migration(root, True))

    def test_guard_a_direct_driver_connect_in_env_py_is_refused(self):
        self.drivers()
        self.refused(sprove.migration(self.alembic_tree(env_py="import psycopg2\npsycopg2.connect('host=x')\n"), True))

    def test_guard_an_engine_for_any_other_database_is_refused_and_one_for_the_throwaway_file_is_allowed(self):
        self.drivers()
        env = "from sqlalchemy import create_engine\nfrom settings import DATABASE_URL\ncreate_engine(DATABASE_URL)\n"
        root = self.alembic_tree(env_py=env)
        write(root, {"alembic/settings.py": "DATABASE_URL = 'postgresql:///app'\n"})
        self.refused(sprove.migration(root, True))
        ok = "from sqlalchemy import create_engine\nfrom alembic import context\n" \
             "create_engine(context.config.get_main_option('sqlalchemy.url'))\n"
        r = sprove.migration(self.alembic_tree(env_py=ok, name="ok"), True)
        self.assertEqual(r["verdict"], "pass", r)
        self.assertTrue(self.connects() and all(x.startswith('["engine", "sqlite:///') for x in self.connects()), self.connects())

    def test_guard_sqlite_outside_the_throwaway_folder_is_refused_inside_it_allowed(self):
        victim = os.path.join(self.tmp, "..", "outside.db")
        r = sprove.migration(self.alembic_tree(env_py=f"import sqlite3\nsqlite3.connect({victim!r})\n"), True)
        self.assertEqual(r["verdict"], "refused", r)
        self.assertFalse(os.path.exists(victim))
        r = sprove.migration(self.alembic_tree(env_py="import sqlite3\nsqlite3.connect('local.db').close()\n", name="in"), True)
        self.assertEqual(r["verdict"], "pass", r)

    def test_guard_django_settings_that_connect_when_loaded_are_refused(self):
        self.drivers()
        root = self.django_tree()
        write(root, {"proj/settings.py": "import psycopg2\npsycopg2.connect('host=x')\nDATABASES = {}\n"})
        self.refused(sprove.migration(root, True))
        self.assertEqual([x for x in self.calls() if x["argv"][0] == "migrate"], [])

    def test_guard_without_a_network_cut_the_result_says_so_and_with_one_it_does_not(self):
        with mock.patch("sampo.infra.sandbox_prefix", lambda *a: []):
            r = sprove.migration(self.alembic_tree(), True)
        self.assertEqual(r["verdict"], "pass", r)
        self.assertIs(r["network_cut"], False)
        self.assertIn("no network cut", r["why"])
        self.stub("cut", "import os, sys\nos.execv(sys.argv[1], sys.argv[1:])\n")
        with mock.patch("sampo.infra.sandbox_prefix", lambda *a: [os.path.join(self.bin, "cut")]):
            r = sprove.migration(self.alembic_tree(name="cut"), True)
        self.assertEqual(r["verdict"], "pass", r)
        self.assertIs(r["network_cut"], True)
        self.assertNotIn("no network cut", r["why"])

    def test_guard_a_script_location_outside_the_tree_is_refused_before_any_run(self):
        self.stub("alembic", ALEMBIC)
        for i, loc in enumerate(("/etc/alembic", "../elsewhere", "%(here)s/../../x")):
            with self.subTest(loc):
                root = self.proj_at(f"loc{i}", {"alembic.ini": f"[alembic]\nscript_location = {loc}\nsqlalchemy.url = sqlite:///d.db\n"})
                r = sprove.migration(root, True)
                self.assertEqual(r["verdict"], "refused", r)
                self.assertIn("script_location", r["why"])
        self.assertEqual(self.calls(), [])


class MigrationFinish(ForemanTestCase):
    """T-2386: fm task finish proves the migrations the task touched, in a trusted tree."""

    def setUp(self):
        super().setUp()
        tools = os.path.join(self.tmp, "tools")
        os.makedirs(tools)
        import shutil
        for need in ("git", "bash"):
            os.symlink(shutil.which(need), os.path.join(tools, need))
        self.env = {"PATH": tools, "FOREMAN_PG_DIRS": NO_PG}  # T-2439: the runner's own Postgres stays out of it
        self.fm("init", env=self.env)
        self.fm("task", "new", "schema change", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step", "s",
                "--focus", env=self.env)

    def trust(self):
        self.fm("prove", "--migration", self.repo, "--yes-run", env=self.env, check=False)

    def finish(self):
        return self.fm("task", "finish", "T-0001", "--audit", "self", "--run", "true", "--lens", "adversary: held",
                       env=self.env, check=False)

    def evidence(self):
        from sampo import core as c
        return [l for l in c.find_brief(c.find_project(self.repo), "T-0001").evidence() if "fm prove --migration" in l]

    def test_migration_finish_records_a_passing_run_and_a_failing_one_refuses_the_close(self):
        self.trust()
        write(self.repo, {"db/migrations/1_users.sql": DBMATE.replace("DROP TABLE users;", "SELECT 1;")})
        r = self.finish()
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("users", r.stdout + r.stderr)
        self.assertTrue(self.evidence())
        write(self.repo, {"db/migrations/1_users.sql": DBMATE})
        r = self.finish()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(any("exit 0" in l and "[ran]" in l for l in self.evidence()), self.evidence())

    def test_migration_finish_an_untrusted_tree_runs_nothing_and_says_how_to_trust_it(self):
        write(self.repo, {"db/migrations/1_users.sql": DBMATE.replace("DROP TABLE users;", "SELECT 1;")})
        r = self.finish()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("--yes-run", r.stdout)
        self.assertEqual(self.evidence(), [])

    def test_migration_finish_unproven_is_said_and_records_no_run_and_other_files_run_nothing(self):
        self.trust()
        write(self.repo, {"db/migrations/1_pg.up.sql": PG_SQL[M + "001_a.up.sql"],
                          "db/migrations/1_pg.down.sql": PG_SQL[M + "001_a.down.sql"]})
        r = self.finish()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("unproven", r.stdout)
        self.assertEqual(self.evidence(), [])
        self.fm("task", "new", "app change", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step", "s",
                "--focus", env=self.env)
        write(self.repo, {"app.py": "x = 1\n"})
        r = self.fm("task", "finish", "T-0002", "--audit", "self", "--run", "true", "--lens", "adversary: held",
                    "--lens", "intent: ok", "--docs", "none: test", "--lesson", "none: test", env=self.env, check=False)
        self.assertNotIn("migration", r.stdout)


class InfraProof(ForemanTestCase):
    """T-2385: fm prove --infra: each class's own validator on a throwaway copy; stubs on PATH stand in for the tools.
    A stub keeps to shell builtins (the PATH here holds only the stubs); it fails a file whose first line says BAD."""
    LIVE = ("apply", "plan", "destroy", "init", "install", "upgrade", "push", "pull", "run", "up", "deploy")

    def setUp(self):
        super().setUp()
        from sampo import core as c
        from unittest import mock
        self.fm("init")
        self.p = c.find_project(self.repo)
        self.bin, self.log = os.path.join(self.tmp, "bin"), os.path.join(self.tmp, "calls.log")
        os.makedirs(self.bin)
        self.tree = os.path.join(self.tmp, "tree")
        write(self.tree, {"Dockerfile": "FROM scratch\n", "Dockerfile.bad": "FROM BAD\n",
                          ".github/workflows/ci.yml": "on: push\n", "notes.txt": "x"})
        self.stub("hadolint")
        patch = mock.patch.dict(os.environ, {"PATH": self.bin, "GITHUB_TOKEN": "t0ps3cret"})
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop("KUBECONFORM_SCHEMAS", None)  # restored by the patch's cleanup

    def stub(self, name, bin_dir=None):
        path = os.path.join(bin_dir or self.bin, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(f'#!/bin/sh\necho "{name} $* pwd=$PWD home=$HOME tok=$GITHUB_TOKEN" >> {self.log}\n'
                    'for last; do :; done\nIFS= read -r l < "$last" 2>/dev/null\n'
                    f'case "$l" in *BAD*) echo "{name}: DL3000 it is bad"; exit 1;; esac\nexit 0\n')
        os.chmod(path, 0o755)

    def calls(self):
        try:
            with open(self.log) as f:
                return f.read().splitlines()
        except OSError:
            return []

    def by_file(self, r):
        return {x["file"]: x for x in r["files"]}

    def test_infra_proof_one_passes_one_fails_one_is_unproven(self):
        r = fmprove_infra.infra(self.tree, allowed=False, p=self.p)
        got = self.by_file(r)
        self.assertEqual(got["Dockerfile"]["verdict"], "pass")
        self.assertEqual(got["Dockerfile.bad"]["verdict"], "fail")
        self.assertIn("DL3000", got["Dockerfile.bad"]["why"])
        self.assertEqual(got[".github/workflows/ci.yml"]["verdict"], "unproven")
        self.assertIn("actionlint", got[".github/workflows/ci.yml"]["why"])
        self.assertNotIn("notes.txt", got)
        self.assertEqual(r["verdict"], "fail")

    def test_infra_proof_runs_on_a_copy_with_a_scrubbed_environment(self):
        mine = lambda: {d for d in os.listdir(tempfile.gettempdir()) if d.startswith("fm-prove-")}  # noqa: E731
        before = mine()
        fmprove_infra.infra(self.tree, allowed=False)
        calls = self.calls()
        self.assertEqual(len(calls), 2, calls)
        tmp = os.path.realpath(tempfile.gettempdir())  # T-2492: a scrubbed env has no PWD, so sh reports the real path
        for call in calls:
            self.assertIn("pwd=" + os.path.join(tmp, "fm-prove-"), call)
            self.assertNotIn(os.path.realpath(self.tree), call)
            self.assertIn("tok=", call)
            self.assertNotIn("t0ps3cret", call)
            self.assertNotIn("home=" + os.environ.get("HOME", "/nonexistent") + " ", call)
        self.assertEqual(mine() - before, set())  # the copy is gone

    def test_infra_proof_copy_under_a_symlinked_temp_dir(self):  # T-2492: every macOS TMPDIR is one (/var -> /private/var)
        real = tempfile.mkdtemp(dir=self.tmp)
        link = real + "-link"
        os.symlink(real, link)
        with mock.patch.object(tempfile, "tempdir", link):
            self.test_infra_proof_runs_on_a_copy_with_a_scrubbed_environment()

    def test_infra_proof_exit_codes_and_what_is_said(self):
        env = {"PATH": self.bin}
        r = self.fm("prove", "--infra", self.tree, check=False, env=env)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("unproven", r.stdout)
        self.assertIn("actionlint", r.stdout)
        os.remove(os.path.join(self.tree, "Dockerfile.bad"))
        self.assertEqual(self.fm("prove", "--infra", self.tree, check=False, env=env).returncode, 0)
        os.remove(os.path.join(self.tree, "Dockerfile"))  # only an unproven file is left: nothing was checked
        self.assertEqual(self.fm("prove", "--infra", self.tree, check=False, env=env).returncode, 2)
        os.remove(os.path.join(self.tree, ".github/workflows/ci.yml"))
        r = self.fm("prove", "--infra", self.tree, "--json", check=False, env=env)
        self.assertEqual((r.returncode, json.loads(r.stdout)["files"]), (2, []))

    def test_infra_proof_a_missing_tool_is_filed_once_for_the_capability_miss_path(self):
        from sampo import core as c
        for _ in range(2):
            fmprove_infra.infra(self.tree, allowed=False, p=self.p)
        mine = [b for b in c.load_briefs(self.p, include_archive=True) if "capability-miss: actionlint" in b.section("Raw request")]
        self.assertEqual(len(mine), 1)

    def test_infra_proof_never_runs_a_live_verb_and_the_table_holds_none(self):
        write(self.tree, {"compose.yaml": "services: {}\n", "main.tf": "x", ".terraform/p": "x", "run.sh": "echo\n",
                          "app.service": "[Unit]\n", "k8s/a.yaml": "kind: Pod\n", "chart/Chart.yaml": "name: c\n"})
        for n in ("docker", "terraform", "shellcheck", "systemd-analyze", "helm", "kubeconform"):
            self.stub(n)
        fmprove_infra.infra(self.tree, allowed=True)
        for call in self.calls():
            self.assertFalse(set(call.split(" pwd=")[0].split()[1:]) & set(self.LIVE), call)
        for k in fmprove_infra.CLASSES.values():
            self.assertFalse(set(k["argv"]) & set(self.LIVE), k["argv"])

    def test_infra_proof_terraform_needs_an_initialised_folder_and_a_trusted_tree(self):
        self.stub("terraform")
        write(self.tree, {"main.tf": "resource {}\n"})
        got = self.by_file(fmprove_infra.infra(self.tree, allowed=True))
        self.assertEqual(got["main.tf"]["verdict"], "unproven")
        self.assertIn("init", got["main.tf"]["why"])
        write(self.tree, {".terraform/providers/x": "plugin"})
        got = self.by_file(fmprove_infra.infra(self.tree, allowed=False))
        self.assertEqual(got["main.tf"]["verdict"], "unproven")
        self.assertIn("--yes-run", got["main.tf"]["why"])
        self.assertFalse([x for x in self.calls() if x.startswith("terraform")])
        got = self.by_file(fmprove_infra.infra(self.tree, allowed=True))
        self.assertEqual(got["main.tf"]["verdict"], "pass")
        (call,) = [x for x in self.calls() if x.startswith("terraform")]
        self.assertIn("validate", call)

    def test_infra_proof_a_helm_chart_is_linted_once_and_kubeconform_needs_local_schemas(self):
        write(self.tree, {"chart/Chart.yaml": "name: c\n", "chart/values.yaml": "a: 1\n", "chart/templates/x.tpl": "x",
                          "values.yaml": "orphan: 1\n", "k8s/pod.yaml": "kind: Pod\n"})
        self.stub("helm")
        self.stub("kubeconform")
        got = self.by_file(fmprove_infra.infra(self.tree, allowed=False))
        self.assertEqual(len([x for x in self.calls() if x.startswith("helm")]), 1, self.calls())
        self.assertEqual(got["chart/Chart.yaml"]["verdict"], "pass")
        self.assertNotIn("values.yaml", got)  # no chart above it: helm lint has nothing to lint
        self.assertEqual(got["k8s/pod.yaml"]["verdict"], "unproven")
        self.assertIn("KUBECONFORM_SCHEMAS", got["k8s/pod.yaml"]["why"])
        self.assertFalse([x for x in self.calls() if x.startswith("kubeconform")])

    def test_infra_proof_the_trees_own_binary_symlinks_and_the_clock_cannot_move_it(self):
        from unittest import mock
        self.stub("hadolint", os.path.join(self.tree, "bin"))
        os.symlink("/etc/hostname", os.path.join(self.tree, "Dockerfile.link"))
        with mock.patch.dict(os.environ, {"PATH": os.path.join(self.tree, "bin")}):
            got = self.by_file(fmprove_infra.infra(self.tree, allowed=False))
        self.assertEqual(got["Dockerfile"]["verdict"], "unproven")  # the tree's hadolint isn't the user's
        self.assertNotIn("Dockerfile.link", got)
        self.assertEqual(self.calls(), [])
        with mock.patch.object(fmprove_infra, "INFRA_T", 0):
            got = self.by_file(fmprove_infra.infra(self.tree, allowed=False))
        self.assertEqual({x["verdict"] for x in got.values()}, {"unproven"})
        self.assertIn("time", got["Dockerfile"]["why"])

    def test_infra_proof_every_class_names_a_card_that_exists_and_has_file_signals(self):
        for name, k in fmprove_infra.CLASSES.items():
            self.assertTrue(fmprove_infra._signals(k), name)

    def test_infra_proof_a_validator_that_says_it_is_not_installed_or_hangs_is_unproven_not_failed(self):
        from unittest import mock
        write(self.tree, {"compose.yaml": "services: {}\n"})
        with open(os.path.join(self.bin, "docker"), "w") as f:
            f.write("#!/bin/sh\necho \"docker: 'compose' is not a docker command.\" >&2\nexit 1\n")
        os.chmod(os.path.join(self.bin, "docker"), 0o755)
        got = self.by_file(fmprove_infra.infra(self.tree, allowed=False))
        self.assertEqual(got["compose.yaml"]["verdict"], "unproven")
        with open(os.path.join(self.bin, "hadolint"), "w") as f:
            # a builtin loop (the PATH here holds no sleep) that ends with its parent: a runner killed before the
            # timeout fires must not leave it spinning under init (T-3028: found at 78% CPU after 1h45)
            f.write("#!/bin/sh\nwhile kill -0 $PPID 2>/dev/null; do :; done\n")
        with mock.patch("sampo.find.TOOL_T", 1):
            got = self.by_file(fmprove_infra.infra(self.tree, allowed=False))
        self.assertEqual(got["Dockerfile"]["verdict"], "unproven")
        self.assertIn("timed out", got["Dockerfile"]["why"])

    def script(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, "w") as f:
            f.write(body)
        os.chmod(path, 0o755)

    def test_infra_proof_a_silent_non_zero_exit_fails_the_file_and_a_tool_that_cannot_start_is_unproven(self):
        self.script("hadolint", "#!/bin/sh\nexit 3\n")
        got = self.by_file(fmprove_infra.infra(self.tree, allowed=False))
        self.assertEqual(got["Dockerfile"]["verdict"], "fail")
        self.assertIn("exit 3", got["Dockerfile"]["why"])
        self.script("hadolint", "#!/nonexistent/interpreter\n")
        got = self.by_file(fmprove_infra.infra(self.tree, allowed=False))
        self.assertEqual(got["Dockerfile"]["verdict"], "unproven")
        self.assertIn("start", got["Dockerfile"]["why"])
        write(self.tree, {"app.service": "[Unit]\n"})  # systemd: only a missing ExecStart binary is benign
        self.script("systemd-analyze", "#!/bin/sh\necho \"/x is not executable: No such file or directory\" >&2\nexit 1\n")
        self.assertEqual(self.by_file(fmprove_infra.infra(self.tree, allowed=False))["app.service"]["verdict"], "pass")
        self.script("systemd-analyze", "#!/bin/sh\nexit 1\n")
        self.assertEqual(self.by_file(fmprove_infra.infra(self.tree, allowed=False))["app.service"]["verdict"], "fail")

    def test_infra_proof_terraform_with_missing_providers_is_unproven_not_failed(self):
        write(self.tree, {"main.tf": "x\n", ".terraform/p": "x"})
        self.script("terraform", "#!/bin/sh\necho 'Error: Missing required provider' >&2\nexit 1\n")
        got = self.by_file(fmprove_infra.infra(self.tree, allowed=True))
        self.assertEqual(got["main.tf"]["verdict"], "unproven")
        self.script("terraform", "#!/bin/sh\necho 'Error: Unsupported argument' >&2\nexit 1\n")
        self.assertEqual(self.by_file(fmprove_infra.infra(self.tree, allowed=True))["main.tf"]["verdict"], "fail")

    def test_infra_proof_a_compose_include_of_a_remote_source_is_unproven_and_not_run(self):
        self.stub("docker")
        write(self.tree, {"compose.yaml": "include:\n  - oci://example.com/stack:1\nservices: {}\n"})
        got = self.by_file(fmprove_infra.infra(self.tree, allowed=False))
        self.assertEqual(got["compose.yaml"]["verdict"], "unproven")
        self.assertFalse([x for x in self.calls() if x.startswith("docker")])
        write(self.tree, {"compose.yaml": "include:\n  - ./other.yaml\nservices: {}\n"})
        self.assertEqual(self.by_file(fmprove_infra.infra(self.tree, allowed=False))["compose.yaml"]["verdict"], "pass")

    def test_infra_proof_style_notes_do_not_fail_a_file(self):
        self.stub("shellcheck")
        write(self.tree, {"run.sh": "echo\n"})
        fmprove_infra.infra(self.tree, allowed=False)
        (hado,) = [x for x in self.calls() if x.startswith("hadolint") and "Dockerfile " in x]
        self.assertIn("--failure-threshold warning", hado)
        (shell,) = [x for x in self.calls() if x.startswith("shellcheck")]
        self.assertIn("-S warning", shell)
        self.assertIn("SC1091", shell)

    def test_infra_proof_the_tool_runs_without_a_network_where_unshare_works_and_plain_where_it_does_not(self):
        self.script("unshare", f'#!/bin/sh\necho "unshare $*" >> {self.log}\nshift 2\nexec "$@"\n')
        fmprove_infra.infra(self.tree, allowed=False)
        self.assertTrue([x for x in self.calls() if x.startswith("unshare -r -n")], self.calls())
        self.assertTrue([x for x in self.calls() if x.startswith("hadolint")], self.calls())
        os.remove(self.log)
        self.script("unshare", "#!/bin/sh\nexit 1\n")  # no user namespaces here
        got = self.by_file(fmprove_infra.infra(self.tree, allowed=False))
        self.assertEqual(got["Dockerfile"]["verdict"], "pass")
        self.assertTrue([x for x in self.calls() if x.startswith("hadolint")])

    def test_infra_proof_a_folder_named_like_an_option_and_a_tree_over_the_copy_cap(self):
        from unittest import mock
        self.stub("terraform")
        write(self.tree, {"--x/main.tf": "x\n", "--x/.terraform/p": "x"})
        fmprove_infra.infra(self.tree, allowed=True)
        (call,) = [x for x in self.calls() if x.startswith("terraform")]
        self.assertIn("-chdir=./--x", call)
        with mock.patch.object(fmprove_infra, "STAGE_MAX", 1):
            got = self.by_file(fmprove_infra.infra(self.tree, allowed=False))
        self.assertEqual(got["Dockerfile"]["verdict"], "unproven")
        self.assertIn("copy", got["Dockerfile"]["why"])

    def test_infra_proof_a_claude_worktree_folder_is_not_looked_at(self):
        write(self.tree, {".claude/worktrees/w/Dockerfile": "FROM BAD\n"})
        got = self.by_file(fmprove_infra.infra(self.tree, allowed=False))
        self.assertEqual(set(got), {"Dockerfile", "Dockerfile.bad", ".github/workflows/ci.yml"})


class InfraFinish(ForemanTestCase):
    """T-2385: fm task finish runs the infra proof on the infra files the task touched."""

    def setUp(self):
        super().setUp()
        self.bin, self.log = os.path.join(self.tmp, "bin"), os.path.join(self.tmp, "calls.log")
        tools = os.path.join(self.tmp, "tools")  # what fm itself needs, and no validator
        os.makedirs(tools)
        os.makedirs(self.bin)
        import shutil
        for need in ("git", "bash"):
            os.symlink(shutil.which(need), os.path.join(tools, need))
        self.env = {"PATH": self.bin + os.pathsep + tools}
        self.fm("init", env=self.env)
        self.fm("task", "new", "infra change", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step", "s",
                "--focus", env=self.env)

    def hadolint(self):
        path = os.path.join(self.bin, "hadolint")
        with open(path, "w") as f:
            f.write(f'#!/bin/sh\necho "$*" >> {self.log}\nfor last; do :; done\nIFS= read -r l < "$last"\n'
                    'case "$l" in *BAD*) echo "DL3000 it is bad"; exit 1;; esac\nexit 0\n')
        os.chmod(path, 0o755)

    def finish(self):
        return self.fm("task", "finish", "T-0001", "--audit", "self", "--run", "true", "--lens", "adversary: held",
                       env=self.env, check=False)

    def evidence(self):
        from sampo import core as c
        return [l for l in c.find_brief(c.find_project(self.repo), "T-0001").evidence() if "fm prove --infra" in l]

    def test_infra_finish_records_a_passing_run_as_evidence_and_a_failing_one_refuses_the_close(self):
        self.hadolint()
        write(self.repo, {"Dockerfile": "FROM BAD\n"})
        r = self.finish()
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("DL3000", r.stdout + r.stderr)
        self.assertTrue(self.evidence())
        write(self.repo, {"Dockerfile": "FROM scratch\n"})
        r = self.finish()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(any("exit 0" in l and "[ran]" in l for l in self.evidence()), self.evidence())

    def test_infra_finish_a_missing_validator_is_said_once_filed_once_and_never_installed(self):
        from sampo import core as c
        write(self.repo, {"Dockerfile": "FROM scratch\n"})
        r = self.finish()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("hadolint", r.stdout)
        self.assertIn("unproven", r.stdout)
        self.assertEqual(self.evidence(), [])  # an unproven file is no run
        self.fm("task", "new", "more infra", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step", "s",
                "--focus", env=self.env)
        write(self.repo, {"Dockerfile": "FROM scratch\n# again\n"})
        self.fm("task", "finish", "T-0002", "--audit", "self", "--run", "true", "--lens", "adversary: held",
                "--lens", "intent: ok", "--docs", "none: test", "--lesson", "none: test", env=self.env)
        mine = [b for b in c.load_briefs(c.find_project(self.repo), include_archive=True)
                if "capability-miss: hadolint" in b.section("Raw request")]
        self.assertEqual(len(mine), 1)
        self.assertEqual(self.calls(), [])

    def test_infra_finish_the_refusal_names_every_failing_file(self):
        self.hadolint()
        write(self.repo, {"Dockerfile": "FROM BAD\n", "Dockerfile.two": "FROM BAD\n", "Dockerfile.three": "FROM scratch\n"})
        r = self.finish()
        self.assertNotEqual(r.returncode, 0)
        out = r.stdout + r.stderr
        self.assertIn("Dockerfile ", out)
        self.assertIn("Dockerfile.two", out)
        self.assertNotIn("Dockerfile.three", out.split("failing (recorded)")[-1])

    def test_infra_finish_a_task_that_touched_no_infra_file_runs_nothing(self):
        self.hadolint()
        write(self.repo, {"app.py": "x = 1\n"})
        self.assertEqual(self.finish().returncode, 0)
        self.assertEqual((self.evidence(), self.calls()), ([], []))

    def calls(self):
        try:
            with open(self.log) as f:
                return f.read().splitlines()
        except OSError:
            return []


def cli(mark, say=""):
    """A stub CLI whose --help lists --out and build; --ghost appears only in this comment (a grep finds it, --help
    doesn't). Running it leaves the marker file, and prints say after a successful parse."""
    return ("#!/usr/bin/env python3\nimport argparse\n# --ghost\n"
            f"open({mark!r}, 'w').close()\n"
            "p = argparse.ArgumentParser(prog='mycli')\ns = p.add_subparsers(dest='cmd', required=True)\n"
            "s.add_parser('build').add_argument('--out')\ns.add_parser('clean')\np.parse_args()\n"
            + (f"print({say!r})\n" if say else ""))


class DocsProof(ForemanTestCase):
    """T-2381: fm prove --docs: the docs' flags, subcommands, paths and fenced commands against the code."""

    def tree(self, readme, mark=None, say=""):
        self.mark = mark or os.path.join(self.tmp, "ran")
        write(self.repo, {"bin/mycli": cli(self.mark, say), "README.md": readme})
        os.chmod(os.path.join(self.repo, "bin", "mycli"), 0o755)
        return self.repo

    def prove(self, *args):
        r = self.fm("prove", "--docs", "--json", *args, check=False)
        return r.returncode, (json.loads(r.stdout) if r.stdout.strip().startswith("{") else r.stdout)

    def by(self, out):
        return {(c["kind"], c["detail"]): c["verdict"] for c in out["claims"]}

    def test_the_text_ends_with_what_it_does_not_prove_and_json_stays_json(self):  # T-2399
        self.tree("Run `mycli --help`.\n")
        text = self.fm("prove", "--docs", check=False).stdout
        self.assertTrue(text.strip().splitlines()[-1].startswith("Does not prove: that the prose is right"), text)
        self.assertIsInstance(self.prove()[1], dict)

    def test_docs_proof_static_a_flag_or_subcommand_no_file_has_fails_and_text_in_a_file_is_unproven(self):
        self.tree("Use `mycli build --out x`, `mycli build --gone` and `mycli frobnicate`.\n"
                  "See `src/missing.py` and `src/mycli.py`.\n")
        write(self.repo, {"src/mycli.py": "x = 1\n"})
        code, out = self.prove()
        got = self.by(out)
        self.assertEqual(code, 1, out)
        self.assertEqual(got[("flag", "--gone")], "failed")
        self.assertEqual(got[("subcommand", "mycli frobnicate")], "failed")
        self.assertEqual(got[("path", "src/missing.py")], "failed")
        self.assertEqual(got[("flag", "--out")], "unproven")  # text in a file is not a working flag
        self.assertEqual(got[("subcommand", "mycli build")], "unproven")
        self.assertIn("text in a file", next(c for c in out["claims"] if c["detail"] == "--out")["why"])
        self.assertEqual(got[("path", "src/mycli.py")], "proven")
        self.assertFalse(os.path.exists(self.mark), "an untrusted tree's command ran")

    def test_docs_proof_static_text_in_a_file_alone_is_not_a_pass(self):
        self.tree("Build:\n\n```\n$ mycli build --out dist\n```\n")
        code, out = self.prove()
        self.assertEqual(code, 2, out)  # nothing failed, but nothing was proven either
        got = self.by(out)
        self.assertEqual(got[("command", "mycli build --out dist")], "unproven")
        self.assertEqual(got[("flag", "--out")], "unproven")
        self.assertFalse(os.path.exists(self.mark))

    def test_docs_proof_static_a_lone_positional_may_be_an_argument_not_a_subcommand(self):
        self.tree("Run `mycli frobnicate`.\n")
        code, out = self.prove()
        self.assertEqual((code, self.by(out)[("subcommand", "mycli frobnicate")]), (2, "unproven"))  # one word: maybe a file name
        self.tree("Run `mycli frobnicate` or `mycli other`.\n")
        self.assertEqual(self.by(self.prove()[1])[("subcommand", "mycli frobnicate")], "failed")

    def test_docs_proof_static_words_after_a_double_dash_are_another_tools(self):
        self.tree("Run `mycli run -- pytest --maxfail=1`.\n")
        got = self.by(self.prove()[1])
        self.assertNotIn(("flag", "--maxfail"), got)

    def test_docs_proof_static_a_flag_from_a_parameter_name_is_in_the_code(self):
        write(self.repo, {"pyproject.toml": "[project.scripts]\napp = 'pkg.cli:main'\n",
                          "pkg/cli.py": "def main(\n    name: str,\n    verbose: bool = False,\n):\n    pass\n",
                          "README.md": "Run `app --verbose` or `app --nope`.\n"})
        code, out = self.prove()
        got = self.by(out)
        self.assertEqual(got[("flag", "--verbose")], "unproven")  # Typer/Fire derive --verbose from the parameter
        self.assertEqual(got[("flag", "--nope")], "failed")

    def test_docs_proof_static_a_bin_shim_that_runs_another_tool_is_not_the_repos_cli(self):
        write(self.repo, {"bin/rails": "#!/usr/bin/env ruby\nAPP_PATH = File.expand_path('../config/application', __dir__)\n"
                                       "require_relative '../config/boot'\nrequire 'rails/commands'\n",
                          "README.md": "Run `rails server --fancy` and `rails about`.\n"})
        os.chmod(os.path.join(self.repo, "bin", "rails"), 0o755)
        code, out = self.prove()
        got = self.by(out)
        self.assertEqual(got.get(("flag", "--fancy"), "unproven"), "unproven")
        self.assertNotIn("failed", got.values())

    def test_docs_proof_prose_and_other_tools_give_nothing_to_check(self):
        self.tree("Some prose about the app. Run `git status`; pass `--force` to git.\n")
        code, out = self.prove()
        self.assertEqual((code, out["claims"]), (2, []), out)

    def test_docs_proof_a_trusted_tree_runs_fenced_commands_and_checks_help(self):
        self.tree("```\nmycli build --out dist\nmycli clean\nmycli build --ghost\nmycli clean --out x\n```\n")
        code, out = self.prove("--yes-run")
        got = self.by(out)
        self.assertEqual(code, 1, out)
        self.assertEqual(got[("command", "mycli build --out dist")], "proven")
        self.assertEqual(got[("command", "mycli clean")], "proven")
        self.assertEqual(got[("command", "mycli build --ghost")], "failed")
        self.assertEqual(got[("flag", "--ghost")], "failed")  # in the code, not in `mycli build --help`
        self.assertEqual(got[("command", "mycli clean --out x")], "failed")
        self.assertEqual(got[("flag", "--out")], "failed")  # a later failure sticks: `mycli clean --help` lacks it
        self.assertTrue(os.path.exists(self.mark), "the trusted run never ran the command")
        code, again = self.prove()  # the yes was kept for the tree
        self.assertEqual(self.by(again)[("command", "mycli build --out dist")], "proven")

    def test_docs_proof_a_run_that_exits_0_with_the_flag_in_help_proves_the_flag_and_subcommand(self):
        self.tree("```\nmycli build --out dist\n```\n")
        code, out = self.prove("--yes-run")
        got = self.by(out)
        self.assertEqual(code, 0, out)
        self.assertEqual((got[("flag", "--out")], got[("subcommand", "mycli build")]), ("proven", "proven"))

    def test_docs_proof_files_limit_what_is_read_and_shell_syntax_is_not_run(self):
        self.tree("`mycli build --gone`\n")
        write(self.repo, {"docs/ok.md": "```\nmycli build | tee log\nmycli build <dir>\nmycli build /etc/passwd\n"
                                        "mycli build ~/x\n```\n"})
        code, out = self.prove("--yes-run", "docs/ok.md")
        self.assertEqual(code, 2, out)  # README's lie is not in the named file; nothing here is run or proven
        self.assertFalse(os.path.exists(self.mark))  # a pipe, a placeholder, an absolute path and ~ are never run
        self.assertNotIn(("flag", "--gone"), self.by(out))
        self.assertEqual({v for (k, _), v in self.by(out).items() if k == "command"}, {"unproven"})
        self.assertEqual(self.fm("prove", "--docs", "../nope.md", check=False).returncode, 2)

    def test_docs_proof_a_folder_then_files_resolve_against_the_folder_without_a_folder_error(self):
        self.tree("`mycli build --gone`\n")
        write(self.repo, {"docs/ok.md": "`mycli build --gone2`\n"})
        r = self.fm("prove", "--docs", "--json", self.repo, "docs/ok.md", check=False, cwd=self.tmp)
        self.assertNotIn("folder", r.stderr)
        got = self.by(json.loads(r.stdout))
        self.assertEqual(got[("flag", "--gone2")], "failed")
        self.assertNotIn(("flag", "--gone"), got)

    def test_docs_proof_yes_run_from_a_subfolder_trusts_that_folder_not_the_whole_repo(self):
        self.tree("`mycli build`\n")
        sub = os.path.join(self.repo, "sub")
        write(self.repo, {"sub/README.md": "`mycli build`\n"})
        self.fm("prove", "--docs", "--yes-run", check=False, cwd=sub)
        self.assertTrue(sprove.trusted(sub))
        self.assertFalse(sprove.trusted(self.repo))

    def test_docs_proof_output_shown_in_the_block_must_be_in_the_run(self):
        self.tree("```\n$ mycli build --out dist\nbuilt dist\n```\n", say="built dist")
        code, out = self.prove("--yes-run")
        got = self.by(out)
        self.assertEqual((code, got[("command", "mycli build --out dist")], got[("flag", "--out")]), (0, "proven", "proven"), out)

    def test_docs_proof_output_that_differs_fails_the_command_and_proves_nothing_it_used(self):
        self.tree("```\n$ mycli build --out dist\nbuilt dist\n```\n", say="removed everything")
        code, out = self.prove("--yes-run")
        got = self.by(out)
        self.assertEqual(code, 1, out)  # exits 0, but does something other than the docs show
        self.assertEqual(got[("command", "mycli build --out dist")], "failed")
        self.assertIn("built dist", next(c for c in out["claims"] if c["kind"] == "command")["why"])
        self.assertEqual((got[("flag", "--out")], got[("subcommand", "mycli build")]), ("unproven", "unproven"))

    def test_docs_proof_output_compares_whitespace_normalised_lines_and_ignores_ansi_codes(self):
        self.tree("```\n$ mycli build\n  built    dist  \n```\n", say="\x1b[32mbuilt\x1b[0m\tdist\nextra line")
        self.assertEqual(self.by(self.prove("--yes-run")[1])[("command", "mycli build")], "proven")

    def test_docs_proof_output_ignores_elisions_and_a_command_without_shown_output_is_unchanged(self):
        self.tree("```\n$ mycli build\nbuilt dist\n...\n```\n\n```\nmycli clean\nmycli build --out x\n```\n", say="built dist")
        got = self.by(self.prove("--yes-run")[1])
        self.assertEqual(got[("command", "mycli build")], "proven")
        self.assertEqual(got[("command", "mycli clean")], "proven")  # the next command is not its output
        self.assertEqual(got[("command", "mycli build --out x")], "proven")

    def test_docs_proof_output_block_right_after_the_command_block_is_matched(self):
        self.tree("```\nmycli build\n```\n\nprints:\n\n```\nbuilt dist\n```\n", say="built dist")
        self.assertEqual(self.by(self.prove("--yes-run")[1])[("command", "mycli build")], "proven")
        self.tree("```\nmycli build\n```\n\nprints:\n\n```\nbuilt dist\n```\n", say="gone")
        self.assertEqual(self.by(self.prove("--yes-run")[1])[("command", "mycli build")], "failed")

    def test_docs_proof_output_is_never_compared_in_an_untrusted_tree(self):
        self.tree("```\n$ mycli build\nbuilt dist\n```\n", say="gone")
        code, out = self.prove()
        self.assertEqual((code, self.by(out)[("command", "mycli build")]), (2, "unproven"))
        self.assertFalse(os.path.exists(self.mark))


BACKFILL = """import sqlite3
db = sqlite3.connect("data/app.db")
db.execute("UPDATE users SET email = name || '@x.test' WHERE id < 3")
db.commit()
"""
BACKFILL_ALL = BACKFILL.replace(" WHERE id < 3", "")
NULLS = "SELECT id FROM users WHERE email IS NULL"


class DataProof(ForemanTestCase):
    """T-2387 (Mission 7, #10): fm prove --data runs a data command on a throwaway copy and checks the declared
    invariants (queries returning the violating rows) before and after; the real database is never touched."""

    def tree(self, name="t", script=BACKFILL, csv=False):
        import sqlite3
        root = os.path.join(self.tmp, name)
        write(root, {"backfill.py": script})
        os.makedirs(os.path.join(root, "data"))
        if csv:
            write(root, {"data/users.csv": "id,name,email\n1,ann,\n2,bob,\n3,cy,\n"})
        else:
            db = sqlite3.connect(os.path.join(root, "data", "app.db"))
            db.execute("CREATE TABLE users (id INTEGER, name TEXT, email TEXT)")
            db.executemany("INSERT INTO users VALUES (?, ?, NULL)", [(1, "ann"), (2, "bob"), (3, "cy")])
            db.commit()
            db.close()
        return root

    def prove(self, root, *args, yes=True):
        return self.fm("prove", root, "--data", *args, *(["--yes-run"] if yes else []), check=False)

    def test_data_invariant_a_backfill_that_leaves_a_null_fails_and_a_clean_one_passes(self):
        import json as j
        root = self.tree()
        before = open(os.path.join(root, "data", "app.db"), "rb").read()
        r = self.prove(root, "--run", "python3 backfill.py", "--check", NULLS, "--json")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        d = j.loads(r.stdout)
        self.assertEqual(d["verdict"], "fail")
        self.assertEqual((d["checks"][0]["before"], d["checks"][0]["after"]), (3, 1))
        self.assertEqual(d["checks"][0]["rows"], [[3]])  # the violating row, shown
        self.assertEqual(d["tables"]["users"], [3, 3])  # each table's row count before and after
        self.assertEqual(open(os.path.join(root, "data", "app.db"), "rb").read(), before)  # the real one untouched
        clean = self.tree("c", BACKFILL_ALL)
        r = self.prove(clean, "--run", "python3 backfill.py", "--check", NULLS)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("pass", r.stdout)

    def test_data_invariant_csv_is_loaded_into_a_throwaway_table_and_a_failed_command_fails(self):
        fill = ("import csv\nrows = list(csv.reader(open('data/users.csv')))\nfor r in rows[1:]:\n    r[2] = r[1] + '@x.test'\n"
                "csv.writer(open('data/users.csv', 'w', newline='')).writerows(rows)\n")
        root = self.tree("csv", fill, csv=True)
        self.assertEqual(self.prove(root, "--run", "python3 backfill.py", "--check", NULLS).returncode, 0)
        r = self.prove(root, "--run", "python3 backfill.py && exit 3", "--check", NULLS)
        self.assertEqual(r.returncode, 1, r.stdout)  # every check is clean, but the command itself failed

    def test_data_invariant_untrusted_or_nothing_to_check_is_exit_two(self):
        root = self.tree()
        r = self.prove(root, "--run", "python3 backfill.py", "--check", NULLS, yes=False)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("--yes-run", r.stdout + r.stderr)
        self.assertEqual(self.prove(root, "--run", "python3 backfill.py").returncode, 2)  # no --check
        self.assertEqual(self.prove(root, "--run", "true", "--check", "SELECT * FROM nope").returncode, 2)  # a check that can't run

    def test_data_isolation_no_connection_variables_a_throwaway_tree_and_no_path_outside(self):
        root = self.tree(script=BACKFILL_ALL)
        cmd = f'test -z "$DATABASE_URL$PGHOST$MYSQL_HOST" && test "$(pwd -P)" != "{os.path.realpath(root)}" && python3 backfill.py'
        r = self.fm("prove", root, "--data", "--run", cmd, "--check", NULLS, "--yes-run", check=False,
                    env={"DATABASE_URL": "postgres://real/prod", "PGHOST": "real", "MYSQL_HOST": "real"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        write(self.tmp, {"outside.db": ""})
        r = self.prove(root, "--run", "true", "--check", NULLS, "--db", "../outside.db")
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("outside", r.stdout + r.stderr)
        r = self.prove(root, "--run", "true", "--check", "SELECT 1; DELETE FROM users")
        self.assertEqual(r.returncode, 2, r.stdout)  # a check reads; it never writes

    def test_data_finish_runs_the_briefs_invariants_and_records_the_run(self):
        import sqlite3
        self.fm("init")
        os.makedirs(os.path.join(self.repo, "data"))
        db = sqlite3.connect(os.path.join(self.repo, "data", "app.db"))
        db.execute("CREATE TABLE users (id INTEGER, name TEXT, email TEXT)")
        db.executemany("INSERT INTO users VALUES (?, ?, NULL)", [(1, "ann"), (2, "bob"), (3, "cy")])
        db.commit()
        db.close()
        write(self.repo, {"backfill.py": BACKFILL})
        self.fm("prove", self.repo, "--yes-run", check=False)  # the user's yes for this tree
        self.fm("task", "new", "backfill emails", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step", "s",
                "--focus")
        self.fm("task", "set", "T-0001", "--section", "Invariants", "--text", f"python3 backfill.py\n{NULLS}")
        write(self.repo, {"backfill.py": BACKFILL + "# touched\n"})
        args = ("task", "finish", "T-0001", "--audit", "self", "--run", "true")
        r = self.fm(*args, check=False)
        self.assertNotEqual(r.returncode, 0, r.stdout)  # one row still has no email
        self.assertIn("fm prove --data", r.stdout + r.stderr)
        write(self.repo, {"backfill.py": BACKFILL_ALL})
        r = self.fm(*args, check=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        brief = self.fm("task", "show", "T-0001").stdout
        self.assertIn("fm prove --data", brief)


PYPROJECT = """[build-system]
requires = ["setuptools"]
build-backend = "{backend}"

[project]
name = "relpkg"
version = "1.2.3"

[project.scripts]
relpkg = "relpkg.{mod}:main"

[tool.setuptools]
packages = ["relpkg"]
"""


class ReleaseProofWithoutSetuptools(ForemanTestCase):  # T-2446: CI's runners have no setuptools; the cases skip there
    def test_release_proof_cases_skip_without_setuptools_where_python3_has_none(self):
        import subprocess
        bindir = os.path.join(self.tmp, "nost")
        os.makedirs(bindir)
        with open(os.path.join(bindir, "python3"), "w") as f:  # -S: no site-packages, so no setuptools, as on CI
            f.write(f"#!/bin/sh\nexec {sys.executable} -S \"$@\"\n")
        os.chmod(os.path.join(bindir, "python3"), 0o755)
        run = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run.py")
        r = subprocess.run([sys.executable, run, "test_prove", "-k", "wheel_that_works", "-k", "left_out_of_the_wheel",
                            "-k", "disagrees_with_the_manifest"], capture_output=True, text=True, timeout=600,
                           env=dict(os.environ, PATH=bindir + os.pathsep + os.environ["PATH"]))
        self.assertEqual(r.returncode, 0, r.stdout[-1500:] + r.stderr[-1500:])  # under -S a pass can only be a skip
        self.assertIn("Ran 3 tests", r.stdout + r.stderr)


NO_SETUPTOOLS = "the python3 on PATH has no setuptools, and fm prove --release builds offline (as on CI's runners)"


class ReleaseProof(ForemanTestCase):
    """T-2389 (Mission 7, #10): fm prove --release builds the manifest's artifact offline in a copy, installs it into a
    throwaway prefix and runs each entry point from there, so a file left out of the artifact fails."""

    def pkg(self, name, mod="cli", backend="setuptools.build_meta", says="relpkg 1.2.3"):
        root = os.path.join(self.tmp, name)
        body = f"def main():\n    print({says!r})\n    return 0\n"
        files = {"pyproject.toml": PYPROJECT.format(backend=backend, mod=mod), "relpkg/__init__.py": ""}
        files["relpkg/tools/cli.py" if mod == "tools.cli" else "relpkg/cli.py"] = body
        if mod == "tools.cli":
            files["relpkg/tools/__init__.py"] = ""
        write(root, files)
        return root

    def need_setuptools(self):
        """T-2446: the cases a setuptools wheel builds skip where the python3 fm prove runs has none."""
        import subprocess
        try:
            ok = subprocess.run(["python3", "-c", "import setuptools"], capture_output=True, timeout=60).returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
        if not ok:
            self.skipTest(NO_SETUPTOOLS)

    def prove(self, root, yes=True):
        import json as j
        r = self.fm("prove", root, "--release", "--json", *(["--yes-run"] if yes else []), check=False, timeout=300)
        return r.returncode, (j.loads(r.stdout) if r.stdout.strip().startswith("{") else {"raw": r.stdout + r.stderr})

    def test_release_proof_a_wheel_that_works_from_its_prefix_passes(self):
        self.need_setuptools()
        code, d = self.prove(self.pkg("good"))
        self.assertEqual((code, d.get("verdict")), (0, "pass"), d)
        self.assertEqual((d["kind"], d["version"]), ("python", "1.2.3"))
        self.assertEqual([e["name"] for e in d["entries"]], ["relpkg"])

    def test_release_proof_an_entry_module_left_out_of_the_wheel_fails(self):
        self.need_setuptools()
        code, d = self.prove(self.pkg("lost", mod="tools.cli"))  # packages = ["relpkg"] leaves relpkg.tools out
        self.assertEqual((code, d.get("verdict")), (1, "fail"), d)
        self.assertIn("relpkg.tools", d["why"])

    def test_release_proof_a_version_that_disagrees_with_the_manifest_fails(self):
        self.need_setuptools()
        code, d = self.prove(self.pkg("old", says="relpkg 1.2.2"))
        self.assertEqual((code, d.get("verdict")), (1, "fail"), d)
        self.assertIn("1.2.3", d["why"])

    def test_release_proof_a_build_that_needs_a_download_is_unproven_naming_it_and_untrusted_is_refused(self):
        code, d = self.prove(self.pkg("dl", backend="fmnope_backend.build"))
        self.assertEqual((code, d.get("verdict")), (2, "unproven"), d)
        self.assertIn("fmnope_backend", d["why"])
        code, d = self.prove(self.pkg("untrusted"), yes=False)
        self.assertEqual((code, d.get("verdict")), (2, "refused"), d)
        write(os.path.join(self.tmp, "none"), {"a.txt": "x"})
        self.assertEqual(self.prove(os.path.join(self.tmp, "none"))[0], 2)  # no manifest: nothing to build

    def test_release_proof_npm_gem_and_cargo_artifacts_run_from_their_prefix(self):
        import shutil as sh
        cases = {
            "npm": {"package.json": '{"name": "relnpm", "version": "2.0.1", "bin": {"relnpm": "cli.js"}}',
                    "cli.js": "#!/usr/bin/env node\nconsole.log('relnpm 2.0.1');\n"},
            "gem": {"relgem.gemspec": 'Gem::Specification.new do |s|\n  s.name = "relgem"\n  s.version = "0.4.0"\n'
                                      '  s.summary = "x"\n  s.authors = ["x"]\n  s.files = ["bin/relgem"]\n'
                                      '  s.executables = ["relgem"]\nend\n',
                    "bin/relgem": "#!/usr/bin/env ruby\nputs 'relgem 0.4.0'\n"},
            "cargo": {"Cargo.toml": '[package]\nname = "relcrate"\nversion = "0.3.0"\nedition = "2021"\n',
                      "src/main.rs": 'fn main() { println!("relcrate 0.3.0"); }\n'}}
        for tool, files in cases.items():
            with self.subTest(tool=tool):
                if not sh.which(tool):
                    self.skipTest(f"no {tool} here")
                root = os.path.join(self.tmp, tool)
                write(root, files)
                code, d = self.prove(root)
                self.assertEqual((code, d.get("verdict")), (0, "pass"), d)


class TaskProveRunner(ForemanTestCase):
    """T-3808: a changed tests-folder file that holds no TestCase and isn't test_*.py (run.py, forkcli.py) is code under
    change: task prove leaves it at its start version, so a run.py change's proof is red on the start tree."""

    def test_runner_is_code_not_a_test(self):
        self.fm("init")
        os.makedirs(os.path.join(self.repo, "tests"), exist_ok=True)
        write(self.repo, {"tests/run.py": "def shards():\n    return 1\n"})
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "runner"], check=True)
        self.fm("task", "new", "Shards", "--type", "FIX", "--tier", "S", "--ac", "s :: true", "--step", "s", "--focus")
        # test_runner imports run: before the fix, prove copied the new run.py onto the start tree, so red passed there
        write(self.repo, {"tests/run.py": "def shards():\n    return 4\n",
                          "tests/test_runner.py": "import run\nassert run.shards() == 4\n"})
        r = self.fm("task", "prove", "T-0001", "--run", "python3 tests/test_runner.py", check=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("proved", r.stdout)
        self.assertIn("kept at its start version: tests/run.py", r.stdout)


if __name__ == "__main__":
    import unittest
    unittest.main()
