"""T-1651: fm design tokens compiles DESIGN.md's ## Tokens table (the one fm design extract writes) to CSS custom
properties, an fm code app theme ([colors] for tui.toml, checked the way fm settings checks it) and the Claude Code
pane's palette (the C object in mods/foreman-ui/hooks/kit.ts)."""
import json
import os
import re
import unittest

from helpers import PLUGIN, ForemanTestCase, headless_chrome, read_text

from sampo import design
from sampo import settings

HERE = os.path.dirname(os.path.abspath(__file__))
DESIGN = os.path.join(HERE, "fixtures", "design-surfaces", "DESIGN.md")
UI = os.path.join(HERE, "fixtures", "design-tokens", "ui")
KIT = os.path.join(os.path.dirname(PLUGIN), "mods", "foreman-ui", "hooks", "kit.ts")


class PaneSaved(ForemanTestCase):  # T-1717
    def test_a_saved_pane_palette_reaches_fm_ui_checked_and_clear_forgets_it(self):
        from sampo import core
        bad = self.fm("design", "tokens", DESIGN, "--to", "css", "--save", check=False)
        self.assertNotEqual(bad.returncode, 0, "--save keeps a pane palette only: never silently ignored")
        self.assertFalse(os.path.exists(core.pane_palette_path()))
        p = self.fm("design", "tokens", DESIGN, "--to", "pane", "--save")
        self.assertIn("Saved for the foreman-ui pane", p.stderr)
        self.fm("init")
        got = json.loads(self.fm("ui", "--json").stdout)["palette_custom"]
        self.assertTrue(got and all(k in core.PANE_ALL and v.startswith("#") for k, v in got.items()), got)
        with open(core.pane_palette_path(), "w") as f:
            json.dump({"accent": "#123456", "evil": "#000000", "ok": "red; x", "err": 7}, f)
        self.assertEqual(json.loads(self.fm("ui", "--json").stdout)["palette_custom"], {"accent": "#123456"})
        p = self.fm("design", "tokens", "--to", "pane", "--clear", env={"FOREMAN_WORKER_OF": "run-1"}, check=False)
        self.assertNotEqual(p.returncode, 0, "a worker never changes the pane's palette")
        self.assertIn("gone", self.fm("design", "tokens", "--to", "pane", "--clear").stdout)
        self.assertEqual(json.loads(self.fm("ui", "--json").stdout)["palette_custom"], {})


class TokenCase(ForemanTestCase):
    def tokens(self, *args, path=DESIGN, check=True):
        return self.fm("design", "tokens", path, *args, check=check)

    def copy(self, old, new):
        path = os.path.join(self.tmp, "DESIGN.md")
        text = read_text(DESIGN)
        self.assertIn(old, text)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text.replace(old, new))
        return path

    def accepted(self, text):
        """What fm settings makes of `text` as the user's tui.toml: ({colors.X: value}, problems), each one read."""
        with open(os.path.join(self.home, "tui.toml"), "w", encoding="utf-8") as f:
            f.write(text)
        problems = []
        got = settings.written(problems)
        colors = {k: v for (s, k), (v, e, read) in got.items() if s == "app" and k.startswith("colors.")}
        unread = [k for (s, k), (v, e, read) in got.items() if s == "app" and not read]
        self.assertEqual(unread, [], text)
        self.assertEqual([p for p in problems if p[0] == "app"], [], text)
        return colors


class Tokens(TokenCase):

    def test_css_variables_light_in_root_dark_in_the_media_query(self):
        css = self.tokens("--to", "css").stdout
        root, dark = css.split("@media (prefers-color-scheme: dark)")
        self.assertIn("--surface: #f2f5f9;", root)
        self.assertIn("--accent: #21539c;", root)
        self.assertIn('--font-body: "Source Sans 3", system-ui, sans-serif;', root)  # every kind, not colours alone
        self.assertIn("--step-0: 1rem;", root)
        self.assertIn("--odd: a | b 'c';", root)  # the table's escaped pipe, unescaped
        self.assertIn("--surface: var(--n-18);", dark)
        self.assertIn("--accent: #83adea;", dark)
        self.assertNotIn("--border", dark)  # no dark value: the light one stands
        self.assertNotIn("--step-0", dark)
        self.assertNotIn("body {", css)  # a value that would close the block is left out, and said
        self.assertRegex(css, r"/\*[^*]*--sneaky[^*]*\*/")
        self.assertNotRegex(css, r"(?m)^\s*--sneaky:")
        self.assertNotIn("--not-a-token", css)  # a table under another heading isn't the Tokens table
        self.assertEqual(css.count("{"), css.count("}"))

    def test_app_theme_is_one_fm_settings_accepts(self):
        toml = self.tokens("--to", "app").stdout
        colors = self.accepted(toml)
        self.assertEqual(colors["colors.screen"]["bg"].upper(), "#0E1216")  # dark values by default
        self.assertEqual(colors["colors.screen"]["fg"].upper(), "#E4E8ED")
        self.assertEqual(colors["colors.strong"]["fg"].upper(), "#E4E8ED")
        self.assertEqual(colors["colors.dim"]["fg"].upper(), "#8C939B")  # --text-muted, not --accent-subtle
        self.assertEqual(colors["colors.accent"]["fg"].upper(), "#83ADEA")
        self.assertTrue(colors["colors.accent"].get("bold"))  # the preset's weight is kept
        self.assertEqual(colors["colors.select"]["bg"].upper(), "#83ADEA")
        self.assertEqual(colors["colors.ok"]["fg"].upper(), "#6FCF8E")
        self.assertEqual(colors["colors.bad"]["fg"].upper(), "#FF8A80")
        self.assertEqual(colors["colors.warn"]["fg"].upper(), "#F5C04A")
        self.assertEqual(colors["colors.border"]["fg"].upper(), "#6B727B")  # light only: it stands for dark too
        self.assertEqual(colors["colors.header"]["bg"].upper(), "#171B1F")
        self.assertEqual(colors["colors.bar"]["bg"].upper(), "#171B1F")
        m = re.search(r"^# not mapped.*: (.+)$", toml, re.M)
        self.assertTrue(m, toml)
        self.assertIn("shade", m.group(1))
        self.assertNotIn("colors.screen", m.group(1))

    def test_light_values_on_the_paper_theme(self):
        toml = self.tokens("--to", "app", "--light").stdout
        self.assertIn('theme = "paper"', toml)
        colors = self.accepted(toml)
        self.assertEqual(colors["colors.screen"]["bg"].upper(), "#F2F5F9")
        self.assertEqual(colors["colors.accent"]["fg"].upper(), "#21539C")

    def test_a_style_too_faint_is_left_out_and_named(self):
        path = self.copy("`#8c939b` #8C939B", "`#1a1e22` #1A1E22")  # muted nearly the screen's own colour
        toml = self.tokens("--to", "app", path=path).stdout
        colors = self.accepted(toml)
        self.assertNotIn("colors.dim", colors)
        self.assertRegex(toml, r"# left out: colors\.dim: .*too faint")
        self.assertEqual(colors["colors.accent"]["fg"].upper(), "#83ADEA")

    def test_pane_palette_uses_the_kits_keys(self):
        pal = json.loads(self.tokens("--to", "pane").stdout)
        kit = read_text(KIT)
        block = re.search(r"export const C = \{(.*?)\n\}", kit, re.S).group(1)
        keys = set(re.findall(r"^\s*(\w+):", block, re.M))
        self.assertLessEqual(set(pal), keys)
        self.assertEqual(pal["accent"].upper(), "#83ADEA")
        self.assertEqual(pal["err"].upper(), "#FF8A80")
        self.assertEqual(pal["muted"].upper(), "#8C939B")
        self.assertEqual(pal["track"].upper(), "#6B727B")
        self.assertTrue(all(re.fullmatch(r"#[0-9A-Fa-f]{6}", v) for v in pal.values()), pal)

    def test_reads_what_extract_writes(self):
        out = os.path.join(self.tmp, "DESIGN.md")
        self.fm("design", "extract", UI, "--out", out)
        css = self.tokens("--to", "css", path=out).stdout
        self.assertIn("--surface: var(--n-97);", css)
        self.assertIn("--n-97: oklch(0.97 0.006 250);", css)
        colors = self.accepted(self.tokens("--to", "app", path=out).stdout)
        self.assertEqual(colors["colors.screen"]["bg"].upper(), "#0E1216")
        self.assertEqual(colors["colors.accent"]["fg"].upper(), "#83ADEA")

    def test_no_tokens_table_is_an_error(self):
        path = os.path.join(self.tmp, "DESIGN.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write("# DESIGN.md\n\n## Direction\n\nCalm.\n")
        p = self.tokens("--to", "css", path=path, check=False)
        self.assertEqual(p.returncode, 2)
        self.assertIn("## Tokens", p.stderr)
        p = self.tokens("--to", "css", path=os.path.join(self.tmp, "none.md"), check=False)
        self.assertEqual(p.returncode, 2)

    def table(self, rows, head="| Token | Kind | Light | Dark | Source |"):
        """A DESIGN.md whose Tokens table holds rows: (name, kind, light cell, dark cell)."""
        path = os.path.join(self.tmp, "T.md")
        lines = ["# DESIGN.md", "", "## Tokens", "", head, "|---|---|---|---|---|"]
        lines += [f"| `{n}` | {k} | {lt} | {dk} | t.css:1 |" for n, k, lt, dk in rows]
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return path

    def test_css_value_that_could_escape_its_declaration_falls_back(self):
        bad = ["calc(1px + 2px", "[x", "x)", '"open', "</style><script>x</script>", "url(https://x.test/a.png)",
               "@import 'x.css'", r"a\62 c", "red; } body { color: red", "a /* x", "x */ y"]
        rows = [(f"--bad-{i}", "other", f"`{v.replace('|', chr(92) + '|')}`", "") for i, v in enumerate(bad)]
        rows += [("--bad-col", "colour", "`url(x)` #112233", "`calc(1px` #445566"),
                 ("--good", "length", "`calc(100% - (2 * var(--step-0)))`", ""),
                 ("--font", "font", "`\"Bob's Sans\", serif`", ""), ("--dark-only", "other", "", "`[x`")]
        css = self.tokens("--to", "css", path=self.table(rows)).stdout
        for i, v in enumerate(bad):
            with self.subTest(v=v):
                self.assertNotRegex(css, rf"(?m)^\s*--bad-{i}:")
                self.assertIn(f"--bad-{i} (light)", css)
        self.assertIn("--bad-col: #112233;", css)  # a colour falls back to its hex
        self.assertIn("--bad-col: #445566;", css)
        self.assertIn("--good: calc(100% - (2 * var(--step-0)));", css)
        self.assertIn("--font: \"Bob's Sans\", serif;", css)
        self.assertIn("--dark-only (dark)", css)  # a dark value refused is said, not dropped
        for bit in ("</style", "url(", "@import", "\\"):
            self.assertNotIn(bit, css.split("/* left out")[0])

    def test_css_dark_values_also_under_the_class_and_data_theme(self):
        css = self.tokens("--to", "css").stdout
        self.assertRegex(css, r'\.dark, \[data-theme="dark"\] \{[^}]*--accent: #83adea;')
        p = self.tokens("--to", "css", "--light")
        self.assertIn("--light", p.stderr)  # said, not silently ignored

    def test_columns_are_read_by_their_header(self):
        path = self.table([("--surface", "`#f2f5f9` #F2F5F9", "`#0e1216` #0E1216", "colour"),  # the table() slots
                           ("--accent", "`#21539c` #21539C", "`#83adea` #83ADEA", "colour")],  # hold Light, Dark, Kind
                          head="| Token | Light | Dark | Kind | Source |")
        css = self.tokens("--to", "css", path=path).stdout
        self.assertIn("--surface: #f2f5f9;", css)
        self.assertIn("--accent: #83adea;", css.split("@media")[1])

    def test_light_token_file_picks_the_paper_preset_and_checks_every_style(self):
        path = self.table([("--surface", "colour", "`#ffffff` #FFFFFF", ""), ("--text", "colour", "`#111111` #111111", ""),
                           ("--accent", "colour", "`#1d4ed8` #1D4ED8", "")])
        toml = self.tokens("--to", "app", path=path).stdout
        self.assertIn('theme = "paper"', toml)  # a white screen: night's pastels would be unreadable on it
        self.accepted(toml)
        self.assertNotRegex(toml, r"# too faint")
        dark = self.tokens("--to", "app", "--dark", path=path).stdout
        self.assertIn('theme = "night"', dark)
        m = re.search(r"^# too faint on this screen.*: (.+)$", dark, re.M)
        self.assertTrue(m, dark)  # night's own styles, never written, checked against the white screen all the same
        self.assertIn("ok", m.group(1))
        self.accepted(dark)

    def test_shadcn_names_map_to_their_roles(self):
        c = lambda lt, dk: (f"`{lt}` {lt.upper()}", f"`{dk}` {dk.upper()}")
        rows = [("--background", "colour", *c("#ffffff", "#0a0a0a")), ("--foreground", "colour", *c("#0a0a0a", "#fafafa")),
                ("--muted", "colour", *c("#f5f5f5", "#262626")),
                ("--muted-foreground", "colour", *c("#737373", "#a3a3a3")),
                ("--accent", "colour", *c("#f5f5f5", "#262626")),
                ("--accent-foreground", "colour", *c("#171717", "#fafafa")),
                ("--primary", "colour", *c("#2563eb", "#60a5fa")),
                ("--primary-foreground", "colour", *c("#fafafa", "#0a0a0a")),
                ("--destructive", "colour", *c("#dc2626", "#f87171")), ("--card", "colour", *c("#ffffff", "#171717")),
                ("--border", "colour", *c("#e5e5e5", "#404040"))]
        colors = self.accepted(self.tokens("--to", "app", path=self.table(rows)).stdout)
        self.assertEqual(colors["colors.screen"]["bg"].upper(), "#0A0A0A")
        self.assertEqual(colors["colors.screen"]["fg"].upper(), "#FAFAFA")
        self.assertEqual(colors["colors.dim"]["fg"].upper(), "#A3A3A3")  # --muted-foreground, not the --muted surface
        self.assertEqual(colors["colors.accent"]["fg"].upper(), "#60A5FA")  # --primary, not the --accent surface
        self.assertEqual(colors["colors.bad"]["fg"].upper(), "#F87171")
        self.assertEqual(colors["colors.header"]["bg"].upper(), "#171717")

    def test_text_muted_beats_a_muted_surface_listed_first(self):
        c = lambda h: (f"`{h}` {h.upper()}", "")
        rows = [("--surface", "colour", *c("#0e1216")), ("--surface-muted", "colour", *c("#1a1e22")),
                ("--text", "colour", *c("#e4e8ed")), ("--text-muted", "colour", *c("#8c939b"))]
        colors = self.accepted(self.tokens("--to", "app", path=self.table(rows)).stdout)
        self.assertEqual(colors["colors.dim"]["fg"].upper(), "#8C939B")

    def test_a_worker_of_fm_code_may_compile(self):
        p = self.fm("design", "tokens", DESIGN, "--to", "pane", env={"FOREMAN_WORKER_OF": "run-1"})
        self.assertIn('"accent"', p.stdout)

    def test_a_worker_may_not_write_with_out(self):  # T-1652 review: --out writes past the run's own write guard
        for args in (("tokens", DESIGN, "--to", "brand", "--out", os.path.join(self.tmp, "b")),
                     ("tokens", DESIGN, "--to", "md", "--out", os.path.join(self.tmp, "t.md")),
                     ("palette", "#3366cc", "--out", os.path.join(self.tmp, "p.md"))):
            r = self.fm("design", *args, env={"FOREMAN_WORKER_OF": "run-1"}, check=False)
            self.assertEqual(r.returncode, 2, args)
            self.assertIn("worker", r.stderr)
            self.assertFalse(os.path.exists(args[-1]), args)

    def test_md_out_onto_its_own_source_is_refused(self):  # T-1652 review: it would drop extract's own lines
        md = self.copy("# DESIGN.md", "# DESIGN.md")
        before = read_text(md)
        self.assertEqual(self.tokens("--to", "md", "--out", md, path=md, check=False).returncode, 2)
        self.assertEqual(read_text(md), before)


class Brand(TokenCase):  # T-1652
    def test_brand_draws_a_favicon_and_a_card_in_the_accent_bg_and_text_roles(self):
        out = os.path.join(self.tmp, "brand")
        self.tokens("--to", "brand", "--out", out)
        fav, card = (read_text(os.path.join(out, n)) for n in ("favicon.svg", "card.svg"))
        self.assertIn("#83ADEA", fav.upper())  # the dark accent unless --light
        self.assertIn('width="1200" height="630"', card)
        self.assertTrue(all(h in card.upper() for h in ("#0E1216", "#E4E8ED", "#83ADEA")), card)  # bg, text, accent
        self.assertIn(">design-surfaces fixture<", card)  # the H1, without its "DESIGN.md:" prefix
        self.tokens("--to", "brand", "--out", out, "--light")
        self.assertIn("#21539C", read_text(os.path.join(out, "favicon.svg")).upper())
        evil = self.copy("# DESIGN.md: design-surfaces fixture", '# <b onload="x">&co')
        self.tokens("--to", "brand", "--out", out, path=evil)
        card = read_text(os.path.join(out, "card.svg"))
        self.assertIn("&lt;b onload=", card)
        self.assertNotIn("<b ", card)
        self.assertEqual(self.tokens("--to", "brand", check=False).returncode, 2, "--to brand needs --out DIR")


DTCG = {"light": {"color": {"accent": {"$type": "color", "$value": "#21539c"},
                            "paper": {"$type": "color", "$value": {"colorSpace": "srgb", "components": [1, 1, 1],
                                                                   "hex": "#ffffff"}},
                            "link": {"$value": "{color.accent}"}},
                  "space": {"$type": "dimension", "s|m`": {"$value": {"value": 4, "unit": "px"}}}},
        "dark": {"color": {"accent": {"$type": "color", "$value": "#83adea"}}}}
FIGMA = {"meta": {"variableCollections": {"c1": {"defaultModeId": "m1", "modes": [{"modeId": "m1", "name": "Light"},
                                                                                  {"modeId": "m2", "name": "Dark"}]}},
                  "variables": {
                      "v1": {"name": "brand/accent", "resolvedType": "COLOR", "variableCollectionId": "c1",
                             "valuesByMode": {"m1": {"r": 0.129, "g": 0.325, "b": 0.612, "a": 1},
                                              "m2": {"r": 0.514, "g": 0.678, "b": 0.918, "a": 1}}},
                      "v2": {"name": "brand/link", "resolvedType": "COLOR", "variableCollectionId": "c1",
                             "valuesByMode": {"m1": {"type": "VARIABLE_ALIAS", "id": "v1"}}},
                      "v3": {"name": "radius/md", "resolvedType": "FLOAT", "variableCollectionId": "c1",
                             "valuesByMode": {"m1": 8, "m2": 8}}}}}


class Imported(TokenCase):  # T-1652: a DTCG or Figma variables export read as tokens
    def put(self, name, data):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(data if isinstance(data, str) else json.dumps(data))
        return path

    def back(self, path):
        """What read_design makes of the ## Tokens table --to md prints for path."""
        md = self.put("DESIGN.md", "# Imported\n\n" + self.tokens("--to", "md", path=path).stdout)
        return design.read_design(md)

    def test_dtcg_reads_as_tokens_and_md_reads_back(self):
        path = self.put("tokens.json", DTCG)
        got = self.back(path)
        self.assertEqual((got["--color-accent"]["light_hex"].upper(), got["--color-accent"]["dark_hex"].upper()),
                         ("#21539C", "#83ADEA"))
        self.assertEqual(got["--color-paper"]["light_hex"].upper(), "#FFFFFF")
        self.assertEqual(got["--color-link"]["light_hex"].upper(), "#21539C", "an alias reads as what it names")
        self.assertEqual(got["--space-s-m"]["light"], "4px", "a name keeps only letters, digits, - and _")
        self.assertIn("--color-accent: #21539c;", self.tokens("--to", "css", path=path).stdout.lower())

    def test_figma_variables_read_by_mode(self):
        got = self.back(self.put("figma.json", FIGMA))
        self.assertEqual((got["--brand-accent"]["light_hex"].upper(), got["--brand-accent"]["dark_hex"].upper()),
                         ("#21539C", "#83ADEA"))
        self.assertEqual(got["--brand-link"]["light_hex"].upper(), "#21539C")
        self.assertEqual(got["--radius-md"]["light"], "8")

    def test_md_out_writes_the_tokens_section_into_a_design_file(self):
        md = self.put("DESIGN.md", "# Mine\n\n## Direction\n\nkeep me\n")
        self.tokens("--to", "md", "--out", md, path=self.put("tokens.json", DTCG))
        self.assertIn("keep me", read_text(md))
        self.assertIn("--color-accent", design.read_design(md))

    def test_groups_beside_light_and_dark_are_shared_and_skips_are_named(self):  # T-1652 review: Tokens Studio
        path = self.put("studio.json", {
            "global": {"blue": {"$type": "color", "$value": "#00f"}, "lift": {"$type": "shadow", "$value": {"x": 1}}},
            "light": {"bg": {"$type": "color", "$value": "{global.blue}"}},
            "dark": {"bg": {"$type": "color", "$value": "#000"}}})
        got = self.back(path)
        self.assertEqual((got["--bg"]["light_hex"].upper(), got["--bg"]["dark_hex"].upper()), ("#0000FF", "#000000"))
        self.assertEqual(got["--global-blue"]["light_hex"].upper(), "#0000FF")
        self.assertIn("1 token(s) skipped", self.tokens("--to", "css", path=path).stderr)

    def test_brand_without_its_colour_roles_exits_2(self):
        path = self.put("bare.json", {"x": {"$type": "dimension", "$value": "4px"}})
        r = self.tokens("--to", "brand", "--out", os.path.join(self.tmp, "b"), path=path, check=False)
        self.assertEqual(r.returncode, 2, r.stdout)

    def test_a_file_that_is_not_tokens_exits_2(self):
        for name, data in (("none.json", {"x": 1}), ("bad.json", "{not json"), ("deep.json", "[" * 5000)):
            r = self.tokens("--to", "css", path=self.put(name, data), check=False)
            self.assertEqual(r.returncode, 2, (name, r.stdout, r.stderr))


class Infer(ForemanTestCase):  # T-1652
    def test_literals_used_twice_become_proposed_rows_or_point_at_their_token(self):
        ui = os.path.join(self.tmp, "ui")
        os.makedirs(ui)
        with open(os.path.join(ui, "app.css"), "w") as f:
            f.write(":root { --brand: #3366cc; }\n"
                    ".a { color: #3366cc; background: #ff0000; transition: opacity 250ms; border-radius: 6px; }\n"
                    ".b { color: #3366CC; background: #f00; transition: opacity 250ms; border-radius: 6px; }\n"
                    ".c { color: #00ff00; }\n")
        r = self.fm("design", "infer", ui)
        self.assertIn("var(--brand)", r.stdout)  # a token already holds it: use that
        md = os.path.join(self.tmp, "DESIGN.md")
        with open(md, "w") as f:
            f.write("# X\n\n" + r.stdout)
        got = design.read_design(md)
        colours = [row["light_hex"].upper() for row in got.values() if row["light_hex"]]
        self.assertEqual(colours, ["#FF0000"], "#3366cc has a token; #00ff00 is used once")
        self.assertEqual(sorted(row["kind"] for row in got.values()), ["colour", "duration", "radius"])
        self.assertEqual(self.fm("design", "infer", os.path.join(self.tmp, "nope"), check=False).returncode, 2)
        got = json.loads(self.fm("design", "infer", ui, "--json").stdout)
        self.assertEqual((sorted(got), got["files"]), (["files", "proposed", "skipped", "use_token"], 1))


class XrayArgs(ForemanTestCase):  # T-1652 review: no browser needed to refuse these
    def test_out_must_name_a_png(self):
        page = os.path.join(self.tmp, "page.html")
        with open(page, "w") as f:
            f.write("<p>x</p>")
        r = self.fm("design", "xray", page, "--tokens", DESIGN, "--out", page, check=False)
        self.assertEqual(r.returncode, 2)
        self.assertEqual(read_text(page), "<p>x</p>")


@unittest.skipUnless(headless_chrome(), "no headless Chromium here")
class Xray(ForemanTestCase):  # T-1652
    def test_xray_outlines_on_and_off_token_elements_and_prints_coverage(self):
        page = os.path.join(self.tmp, "page.html")
        with open(page, "w") as f:
            f.write('<!doctype html><html><body style="margin:0;background:#f2f5f9;color:#171b1f">'
                    '<p>on token</p><p style="color:#ff0000">off token</p></body></html>')
        shot = os.path.join(self.tmp, "x.png")
        r = self.fm("design", "xray", page, "--tokens", DESIGN, "--out", shot, "--json", check=False)
        self.assertEqual(r.returncode, 1, r.stderr)  # an off-token element
        got = json.loads(r.stdout)
        self.assertEqual((got["on"], got["off"], got["off_colours"]), (2, 1, {"#FF0000": 1}), got)
        with open(shot, "rb") as f:
            self.assertEqual(f.read(8), b"\x89PNG\r\n\x1a\n")
        self.assertIn("2 of 3", self.fm("design", "xray", page, "--tokens", DESIGN, check=False).stdout)
        with open(page, "w") as f:
            f.write("<!doctype html><title>t</title>")
        r = self.fm("design", "xray", page, "--tokens", DESIGN, check=False)
        self.assertEqual(r.returncode, 2, "nothing painted is not 100% on token")
        self.assertIn("nothing painted", r.stderr)


class DesignPlugin(unittest.TestCase):  # T-1652: the design skills as an optional plugin
    def test_the_foreman_design_entry_resolves(self):
        root = os.path.dirname(PLUGIN)
        mk = json.loads(read_text(os.path.join(root, ".claude-plugin", "marketplace.json")))
        entry = next(p for p in mk["plugins"] if p["name"] == "sampo-design")
        base = os.path.normpath(os.path.join(root, entry["source"]))
        self.assertEqual(json.loads(read_text(os.path.join(base, ".claude-plugin", "plugin.json")))["name"],
                         "sampo-design")
        skills = sorted(os.listdir(os.path.join(base, "skills")))
        self.assertEqual(skills, ["design-extract", "directions", "ui-design"])
        for s in skills:
            link = os.path.join(base, "skills", s)
            self.assertTrue(os.path.islink(link) and os.path.isfile(os.path.join(link, "SKILL.md")), s)
            self.assertTrue(os.path.realpath(link).startswith(os.path.join(os.path.realpath(PLUGIN), "skills")), s)


class PaneForge(unittest.TestCase):  # T-2853: the pane wears the forge (the illustrated-forge theme, 2026-11-03)
    TOKENS = os.path.join(os.path.dirname(PLUGIN), "design", "sampo", "tokens.json")

    def palette(self):
        text = read_text(KIT)
        body = text[text.index("export const C = {"):text.index("}", text.index("export const C = {"))]
        c = {k: int(v, 16) for k, v in re.findall(r"^\s*(\w+):\s*0x([0-9a-fA-F]{6})", body, re.M)}
        d = text[text.index("default: {"):text.index("}", text.index("default: {"))]
        return c, {k: int(v, 16) for k, v in re.findall(r"(\w+):\s*0x([0-9a-fA-F]{6})", d)}

    @staticmethod
    def lum(x):
        ch = [((x >> s) & 255) / 255 for s in (16, 8, 0)]
        ch = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in ch]
        return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2]

    def ratio(self, a, b):
        x, y = sorted((self.lum(a), self.lum(b)), reverse=True)
        return (x + 0.05) / (y + 0.05)

    def test_the_default_palette_is_the_forge_and_reads_on_a_dark_terminal(self):
        c, default = self.palette()
        night = json.loads(read_text(self.TOKENS))["dark"]["color"]
        tok = lambda k: int(night[k]["$value"][1:], 16)
        self.assertEqual((c["accent"], c["ok"], c["warn"], c["err"]), (tok("glow"), tok("success"), tok("warning"),
                                                                       tok("error")))
        self.assertEqual(c["muted"], tok("text-muted"))
        self.assertEqual({k: c[k] for k in default}, default, "setPalette('default') puts the same colours back")
        for k in ("accent", "accent2", "ok", "warn", "err", "edit", "agent", "web", "skill", "muted"):
            for bg in (0x1E1E1E, 0x060819):  # a common dark terminal, and the forge's night
                self.assertGreaterEqual(self.ratio(c[k], bg), 4.5, f"{k} on #{bg:06x}")
        far = lambda a, b: sum((((a >> s) & 255) - ((b >> s) & 255)) ** 2 for s in (16, 8, 0)) ** 0.5
        for a, b in (("accent", "accent2"), ("accent", "warn"), ("ok", "err"), ("err", "edit"), ("skill", "err"),
                     ("accent", "skill"), ("agent", "accent2"), ("ok", "web")):
            self.assertGreater(far(c[a], c[b]), 60, f"{a} and {b} read apart")
