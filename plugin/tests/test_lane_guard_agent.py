"""T-4340: the main session works in a builder's lane (Claude Code's .claude/worktrees/agent-X) once the lane's task
is known there: a focus the lane's own plugin recorded, a merge at the lane's root, a refusal that names what it read."""
import json
import os
import re
import subprocess
import unittest

from helpers import ForemanTestCase


class LaneGuardAgent(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")

    def planned(self, title):
        out = self.fm("task", "new", title, "--type", "FIX", "--tier", "S", "--ac", "ok :: true", "--step", "s",
                      "--json").stdout
        return json.JSONDecoder().raw_decode(out)[0]["id"]

    def lane(self, name="agent-x1y2", branch=None):
        path = os.path.join(self.repo, ".claude", "worktrees", name)
        branch = branch or "worktree-" + re.sub(r"[^\w-]", "-", name)
        subprocess.run(["git", "-C", self.repo, "worktree", "add", "-q", "-b", branch, path],
                       check=True)
        return path

    def from_main(self, tool, **ti):
        """A call of the main session: its cwd is the main checkout, as a Bash cd does not move it."""
        return self.hook("PreToolUse", {"tool_name": tool, "cwd": self.repo, "tool_input": ti})

    def write(self, path):
        return self.from_main("Write", file_path=path, content="x")

    def bash(self, cmd):
        return self.from_main("Bash", command=cmd)

    def active(self, cwd):
        return (json.loads(self.fm("state", "--json", cwd=cwd).stdout).get("active") or {}).get("id")

    def set_meta(self, tid, **kv):
        """Edit a brief's frontmatter in place: a stale or missing record, as a recovered lane has."""
        from sampo import core as c
        p = c.find_project(self.repo)
        b = c.find_brief(p, tid)
        for k, v in kv.items():
            if v is None:
                b.meta.pop(k, None)
            else:
                b.meta[k] = v
        c.save_brief(p, b)

    # -- case a: focused by the lane's own plugin, the main session's edit in the lane

    def test_a_lane_task_focused_by_lane_copy_lets_the_main_sessions_edit_through(self):
        tid = self.planned("Recovered lane")
        lane = self.lane()
        self.fm("focus", tid, cwd=lane)  # what the lane's own plugin does there: Focus: T-ID
        self.assertEqual((self.active(lane), self.active(self.repo)), (tid, None))
        p = self.write(os.path.join(lane, "x.py"))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        open(os.path.join(lane, "ANDON.md"), "w").close()
        p = self.bash(f"rm {lane}/ANDON.md")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_the_focused_lane_frees_only_its_own_files(self):
        tid = self.planned("Recovered lane")
        lane = self.lane()
        self.fm("focus", tid, cwd=lane)
        for cmd in (f"rm {self.repo}/README.md", f"rm {lane}/x.py {self.repo}/README.md", f"rm -rf {lane}"):
            p = self.bash(cmd)
            self.assertEqual(p.returncode, 2, cmd + p.stdout)
        p = self.write(os.path.join(self.repo, "y.py"))
        self.assertEqual(p.returncode, 2, p.stdout)
        self.assertIn("no active task in this project", p.stdout)

    # -- case b: the Agent tool's lane, whose brief names it

    def test_a_merge_at_an_agent_lanes_root_goes_through_when_its_task_is_active_there(self):
        tid = self.planned("Builder done")
        lane = self.lane()
        self.fm("focus", tid, cwd=lane)
        subprocess.run(["git", "-C", self.repo, "branch", "sibling"], check=True)
        p = self.bash(f"cd {lane} && git merge sibling")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_agent_lane_planned_task_is_focused_by_the_main_sessions_edit(self):
        tid = self.planned("Builder paused")
        lane = self.lane()
        self.fm("focus", tid, cwd=lane)  # its brief records the lane (and the branch)
        self.fm("task", "block", tid, "waits", cwd=lane)
        self.fm("task", "unblock", tid, cwd=lane)
        self.assertEqual(self.active(lane), None)
        p = self.write(os.path.join(lane, "x.py"))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.active(lane), tid)  # focused there, not in the main checkout
        self.assertIsNone(self.active(self.repo))

    def test_agent_lane_known_by_its_branch_alone_is_focused_too(self):
        tid = self.planned("Branch only")
        lane = self.lane(branch=f"foreman/{tid}")  # the branch names the task: no record needed
        p = self.write(os.path.join(lane, "x.py"))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual((self.active(lane), self.active(self.repo)), (tid, None))

    # -- what stays refused

    def test_a_done_tasks_lane_still_refuses(self):
        tid = self.planned("Done work")
        lane = self.lane()
        self.fm("focus", tid, cwd=lane)
        self.set_meta(tid, status="done")
        for p in (self.write(os.path.join(lane, "x.py")), self.bash(f"cd {lane} && git merge HEAD~0 -m x")):
            self.assertEqual(p.returncode, 2, p.stdout)

    def test_a_plain_folder_under_worktrees_still_refuses(self):
        tid = self.planned("Elsewhere")
        plain = os.path.join(self.repo, ".claude", "worktrees", "plain")
        os.makedirs(plain)
        self.set_meta(tid, lane=plain, status="active")  # a record naming a folder that is no worktree's top
        for p in (self.write(os.path.join(plain, "x.py")), self.bash(f"cd {plain} && git merge HEAD -m x")):
            self.assertEqual(p.returncode, 2, p.stdout)
            self.assertIn("no active task", p.stdout)

    def test_a_lane_with_two_open_tasks_still_refuses(self):
        a, b = self.planned("First"), self.planned("Second")
        lane = self.lane()
        self.fm("focus", a, cwd=lane)
        self.fm("focus", b, cwd=lane)  # a is paused: planned, still recording the lane; b active there
        self.assertEqual(self.active(lane), b)
        for p in (self.write(os.path.join(lane, "x.py")), self.bash(f"cd {lane} && git merge HEAD -m x")):
            self.assertEqual(p.returncode, 2, p.stdout)

    def test_a_lane_task_active_in_the_main_checkout_still_refuses_the_lane(self):
        tid = self.planned("Main took it")
        self.fm("focus", tid)  # active in the main checkout: the main session then has a task of its own
        lane = self.lane(branch=f"foreman/{tid}")
        # the main session is briefed, so it already passes (as before): the lane is not made freer or stricter
        p = self.write(os.path.join(lane, "x.py"))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    # -- review round: what the exemption must not reach

    def focused(self, name="agent-x1y2", title="Focused lane"):
        tid = self.planned(title)
        lane = self.lane(name)
        self.fm("focus", tid, cwd=lane)
        return tid, lane

    def test_a_symlink_in_the_lane_does_not_carry_an_edit_out_of_it(self):
        tid, lane = self.focused()
        sub = os.path.join(self.repo, "sub")
        os.makedirs(sub)
        os.symlink(sub, os.path.join(lane, "link"))  # the main session may make this link: it is inside the lane
        p = self.bash(f"ln -s {sub} {lane}/another")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        for p in (self.write(f"{lane}/link/../x.py"),  # the kernel goes through the link, then up: the main checkout
                  self.write(f"{lane}/link/x.py"),
                  self.bash(f"rm {lane}/link/../README.md"),
                  self.bash(f"cd {lane}/link && cd .. && rm README.md"),
                  self.bash(f"cd {lane}/link && rm ../README.md")):
            self.assertEqual(p.returncode, 2, p.stdout)
        self.assertEqual(self.write(f"{lane}/fine/../x.py").returncode, 2)  # a .. is never read as inside: refused
        self.assertEqual(self.write(f"{lane}/x.py").returncode, 0)

    def test_the_lanes_git_pointer_and_anything_under_a_dot_git_stay_refused(self):
        tid, lane = self.focused()
        for p in (self.write(f"{lane}/.git"), self.write(f"{lane}/.git/hooks/pre-commit"),
                  self.write(f"{lane}/sub/.git/config"), self.bash(f"rm {lane}/.git"),
                  self.bash(f"echo x > {lane}/.git"), self.bash(f"rm -rf {lane}/.git/hooks")):
            self.assertEqual(p.returncode, 2, p.stdout)
        self.assertEqual(self.write(f"{lane}/.gitignore").returncode, 0)  # a name that only starts with it

    def test_a_builder_still_running_keeps_the_main_session_out_of_its_lane(self):
        from sampo import core as c
        tid = self.planned("Builder at work")
        lane = self.lane()
        self.fm("focus", tid, cwd=lane)
        self.set_meta(tid, builder=c.now())  # fm lane brief marked the launch
        p = self.write(f"{lane}/x.py")
        self.assertEqual(p.returncode, 2, p.stdout)
        self.assertIn("its builder is still running", p.stdout)
        self.assertIn(tid, p.stdout)
        p = self.bash(f"cd {lane} && git merge HEAD")
        self.assertEqual(p.returncode, 2, p.stdout)
        self.set_meta(tid, builder_returned=c.now())  # its own stop said it is done (T-3145)
        for p in (self.write(f"{lane}/x.py"), self.bash(f"cd {lane} && git merge HEAD")):
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_a_task_active_in_another_lane_is_not_re_homed_by_the_branch_match(self):
        tid = self.planned("Lives elsewhere")
        there = self.lane("agent-there")
        self.fm("focus", tid, cwd=there)  # recorded in a lane that exists
        here = self.lane("agent-here", branch=f"foreman/{tid}")  # this one only shares the branch name
        p = self.write(f"{here}/x.py")
        self.assertEqual(p.returncode, 2, p.stdout)
        self.assertEqual((self.active(there), self.active(here)), (tid, None))
        from sampo import core as c
        self.assertEqual(c.find_brief(c.find_project(self.repo), tid).meta.get("lane"), there)

    def test_a_lane_folder_with_a_space_and_a_bracket_in_its_name_is_read_whole(self):
        tid, lane = self.focused("agent-a (b)")
        for p in (self.write(f"{lane}/x.py"), self.bash(f"cd '{lane}' && git merge HEAD")):
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.write(f"{self.repo}/y.py").returncode, 2)

    def test_a_lane_name_that_could_be_two_folders_is_refused(self):
        tid = self.planned("Ambiguous")
        self.lane("agent-a")
        lane = self.lane("agent-a (b)")
        self.fm("focus", tid, cwd=lane)
        self.assertEqual(self.write(f"{lane}/x.py").returncode, 2)

    def test_the_lane_is_not_focused_when_the_command_also_writes_the_main_checkout(self):
        tid = self.planned("Not focused")
        lane = self.lane(branch=f"foreman/{tid}")
        for cmd in (f"rm {lane}/x.py {self.repo}/README.md", f"touch {lane}/x.py && rm {self.repo}/README.md"):
            p = self.bash(cmd)
            self.assertEqual(p.returncode, 2, cmd + p.stdout)
            self.assertIsNone(self.active(lane), cmd)  # no side effect for a command that is then refused
        p = self.bash(f"touch {lane}/x.py")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.active(lane), tid)

    def test_what_a_lane_task_never_frees(self):
        tid, lane = self.focused()
        other = os.path.join(self.repo, "README.md")
        subprocess.run(["git", "-C", self.repo, "branch", "b"], check=True)
        bare = self.lane("agent-bare")  # a lane with no task: a -C into it is no merge of this lane
        link = os.path.join(self.repo, ".claude", "worktrees", "agent-link")
        os.symlink(lane, link)  # a link to the lane is no lane: its top is another folder
        for cmd in (f"echo x > {link}/x.py",
                    f"cd {lane}/.. && rm x.py",
                    f"rm {lane}/../../../README.md",
                    f"cd {lane} && git merge b; rm {other}",
                    f"cd {lane} || true; git merge b",
                    f"cd {lane}; cd ..; git merge b",
                    f"cd {lane} && git checkout .",
                    f"cd {lane} && git reset --hard",
                    f"cd {lane} && git clean -fd",
                    f"cd {lane} && git worktree remove .",
                    f"cd {lane} && git worktree remove --force {lane}",
                    f"cd {lane} && git -C {bare} merge b",  # (a -C to the main checkout is the project's own merge)
                    f"git -C {bare} merge b",
                    f"cd {lane} && git --work-tree={bare} merge b",
                    f"cd {lane} && git --git-dir={bare}/.git merge b",
                    f"cd {lane} && git merge --into-name x b",
                    f"cd {lane} && git merge --squash b -m x",
                    f"cd {lane} && GIT_DIR={self.repo}/.git git merge b"):
            p = self.bash(cmd)
            self.assertEqual(p.returncode, 2, cmd + "\n" + p.stdout + p.stderr)
        p = self.write(link + "/x.py")
        self.assertEqual(p.returncode, 2, p.stdout)
        p = self.write(f"{lane}/../x.py")
        self.assertEqual(p.returncode, 2, p.stdout)

    def test_a_subagent_does_not_reach_another_builders_lane(self):
        from sampo import core as c
        tid, mine = self.focused("agent-mine")
        t2, theirs = self.focused("agent-theirs", "Theirs")
        self.set_meta(tid, builder=c.now())  # mine is a builder's lane: its session is confined to it
        pl = {"tool_name": "Write", "cwd": mine, "agent_id": "mine", "tool_input": {"file_path": f"{theirs}/x.py",
                                                                                      "content": "x"}}
        self.assertEqual(self.hook("PreToolUse", pl).returncode, 2)
        for env in ({"FOREMAN_WORKER_OF": tid}, {"FOREMAN_WORKER_OF": tid, "FOREMAN_QUIET": "1"}):
            p = self.hook("PreToolUse", dict(pl, agent_id="w"), env=env)
            self.assertEqual(p.returncode, 2, f"{env}: {p.stdout}")
        pl["tool_input"]["file_path"] = f"{mine}/x.py"  # its own lane stays open to it
        self.assertEqual(self.hook("PreToolUse", pl).returncode, 0)

    def test_looked_names_a_different_task_active_in_the_lane(self):
        from sampo import core as c, hooks
        tid, lane = self.focused()
        other = self.planned("Took the lane meanwhile")
        self.set_meta(other, lane=lane, status="active")
        self.set_meta(tid, status="planned")
        text = hooks._lane_looked(c.find_project(self.repo), lane, tid, "ran without error")
        self.assertIn(f"{other} is active there", text)
        self.assertIn(tid, text)

    # -- the refusal's advice

    def test_advice_for_a_task_already_active_says_what_the_guard_read_and_never_loops(self):
        tid = self.planned("Stale record")
        lane = self.lane(branch=f"foreman/{tid}")
        self.fm("focus", tid, cwd=lane)
        stale = os.path.join(self.repo, ".claude", "worktrees", "agent-gone")
        self.set_meta(tid, lane=stale)  # active, but its record names a folder that is not this lane
        p = self.write(os.path.join(lane, "x.py"))
        self.assertEqual(p.returncode, 2, p.stdout)
        self.assertNotIn(f"&& sampo focus {tid}, then retry", p.stdout)  # the focus advice just followed
        self.assertIn("looked for", p.stdout)
        self.assertIn(tid, p.stdout)
        self.assertIn(stale, p.stdout)  # what the record says
        self.assertIn(lane, p.stdout)

    def test_advice_names_the_command_when_the_lane_has_no_task(self):
        lane = self.lane()
        p = self.write(os.path.join(lane, "x.py"))
        self.assertEqual(p.returncode, 2, p.stdout)
        self.assertIn(f"no active task in lane {lane}", p.stdout)
        self.assertIn("sampo focus ID", p.stdout)


if __name__ == "__main__":
    unittest.main()
