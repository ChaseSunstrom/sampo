"""T-1785: every UI playbook names a worked example page on an `Example:` line; the file ships beside the playbooks,
keeps fm new check's rules (fm design lint) and loads nothing from another host; site/build.py copies each to
gallery/NAME.html and the Gallery feature page links every one."""
import os
import re
import tempfile
import unittest

from helpers import PLUGIN, read_text
from test_site_build import ROOT, site

BOOKS = os.path.join(PLUGIN, "skills", "playbooks")
UI = os.path.join(BOOKS, "references", "ui")


def playbooks():
    return sorted(n[:-3] for n in os.listdir(UI) if n.endswith(".md") and n != "INDEX.md")


class Gallery(unittest.TestCase):
    def test_an_xml_namespace_in_a_script_is_not_a_load(self):
        build = site("build")
        ns = '<script>document.createElementNS("http://www.w3.org/2000/svg", "path")</script>'
        self.assertEqual(build.outside_loads(ns, ".html"), [], "a namespace URI is a name the browser never fetches")
        fetch = '<script>fetch("http://www.w3.org/2000/svg.js")</script>'
        self.assertEqual(build.outside_loads(fetch, ".html"), ["http://www.w3.org/2000/svg.js"])
        img = '<img src="http://www.w3.org/2000/svg">'
        self.assertEqual(build.outside_loads(img, ".html"), ["http://www.w3.org/2000/svg"], "a src is fetched")

    def test_an_example_path_stays_in_the_library(self):
        pages = site("pages")
        real = pages._books

        def fake(stage):
            return [("x", "X", "", "Example: `references/ui/../../../etc/x.html`")]
        pages._books = fake
        try:
            with self.assertRaises(SystemExit):
                pages.gallery()
        finally:
            pages._books = real

    def test_every_ui_playbook_names_an_example_that_keeps_the_rules(self):
        from sampo import new
        build, pages = site("build"), site("pages")
        found = {name: src for name, _, _, src in pages.gallery()}
        for name in playbooks():
            with self.subTest(name):
                line = re.search(r"(?m)^Example: `(references/ui/[\w./-]+\.html)`", read_text(os.path.join(UI, name + ".md")))
                self.assertTrue(line, f"{name}.md names no example (an `Example: \\`references/ui/...html\\`` line)")
                path = os.path.join(BOOKS, line.group(1))
                self.assertTrue(os.path.isfile(path), f"{line.group(1)} doesn't exist")
                self.assertEqual(found.get(name), path, "the gallery reads the same line")
                self.assertEqual(new.check(path), [], "it keeps fm new check's rules")
                html = read_text(path)
                self.assertEqual(build.outside_loads(html, ".html"), [], "it loads nothing from another host")
                for need in ("<!doctype html>", '<html lang="', 'name="viewport"', "<title>"):
                    self.assertIn(need, html.lower() if need.startswith("<!") else html)

    def test_the_build_copies_each_example_and_the_gallery_page_links_it(self):
        build, pages = site("build"), site("pages")
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "_site")
            build.build(out)
            page = read_text(os.path.join(out, "features", "gallery.html"))
            self.assertIn('href="gallery.html"', read_text(os.path.join(out, "features", "index.html")))  # T-3996: the grouped index
            for name, _, _, src in pages.gallery():
                self.assertEqual(read_text(os.path.join(out, "gallery", name + ".html")), read_text(src))
                self.assertIn(f'href="../gallery/{name}.html"', page)
            self.assertEqual(build.outside_hosts(out), [])
        self.assertEqual(sorted(n for n, *_ in pages.gallery()), playbooks(), "one card per UI playbook")


if __name__ == "__main__":
    unittest.main()
