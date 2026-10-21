"""End-to-end tests for the panel's parser (sidecars/context/context_report.py) on synthetic conversation databases.

Run with: python -m unittest discover tests
"""

import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parent.parent / "sidecars" / "context" / "context_report.py"

spec = importlib.util.spec_from_file_location("context_report", SCRIPT)
context_report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(context_report)

# --- protobuf encoding -------------------------------------------------------


def varint(n: int) -> bytes:
    n &= 0xFFFFFFFFFFFFFFFF
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out += bytes([b | 0x80])
        else:
            return out + bytes([b])


def f(fn: int, val) -> bytes:
    if isinstance(val, int):
        return varint(fn << 3) + varint(val)
    if isinstance(val, str):
        val = val.encode()
    return varint(fn << 3 | 2) + varint(len(val)) + val


def m(*parts: bytes) -> bytes:
    return b"".join(parts)


# --- database builder --------------------------------------------------------


class ConversationDB:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path)
        self.conn.execute(
            "CREATE TABLE steps (idx integer, step_type integer NOT NULL DEFAULT 0, status integer NOT NULL DEFAULT 0, "
            "has_subtrajectory numeric NOT NULL DEFAULT false, metadata blob, error_details blob, permissions blob, "
            "task_details blob, render_info blob, step_payload blob, step_format integer NOT NULL DEFAULT 0, PRIMARY KEY (idx))"
        )
        self.conn.execute("CREATE TABLE gen_metadata (idx integer, data blob, size integer NOT NULL DEFAULT 0, PRIMARY KEY (idx))")
        self.n = 0

    def step(self, step_type: int, metadata: bytes = b"", payload_field: int | None = None, payload: bytes = b"", status: int = 3):
        pb = m(f(1, step_type), f(4, status), f(5, metadata), f(payload_field, payload) if payload_field else b"")
        self.conn.execute(
            "INSERT INTO steps (idx, step_type, status, metadata, step_payload) VALUES (?, ?, ?, ?, ?)",
            (self.n, step_type, status, metadata, pb),
        )
        self.n += 1

    def generation(self, groups: list[bytes], max_context: int = 1_000_000, usage: bytes = b"", checkpoint_index: int = -1,
                   model: str = "test-model", display_name: str = "Test Model"):
        cwm = m(f(1, 0), f(3, m(*[f(1, g) for g in groups])), f(4, max_context))
        chat = m(f(4, usage), f(9, m(f(2, checkpoint_index), f(10, cwm))), f(21, display_name) if display_name else b"")
        data = m(f(1, chat), f(3, f(28, model)))
        self.conn.execute("INSERT INTO gen_metadata (idx, data) VALUES ((SELECT COUNT(*) FROM gen_metadata), ?)", (data,))

    def close(self):
        if self.conn is not None:
            self.conn.commit()
            self.conn.close()
            self.conn = None


def group(token_type: int, tokens: int, *children: tuple[str, int], name: str = "group") -> bytes:
    return m(f(1, name), f(2, token_type), f(4, tokens), *[f(5, m(f(1, n), f(3, t))) for n, t in children])


def user_input(text: str) -> bytes:
    return f(2, text)


def planner(text: str) -> bytes:
    return f(1, text)


def usage(input_tokens: int = 0, output: int = 0, cache_read: int = 0) -> bytes:
    return m(f(2, input_tokens), f(3, output), f(5, cache_read))


def tool_call(args: str) -> bytes:
    return f(4, m(f(2, "tool"), f(3, args)))


# Step types and payload fields used below.
USER_INPUT, PLANNER_RESPONSE, CHECKPOINT, FINISH, VIEW_FILE, CODE_ACTION, GENERIC = 14, 15, 23, 2, 8, 5, 132
P_USER, P_PLANNER, P_CHECKPOINT, P_VIEW_FILE, P_CODE_ACTION = 19, 20, 30, 14, 10


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.db_path = self.home / "conv.db"
        self.db = ConversationDB(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def run_report(self, *args: str) -> str:
        self.db.close()
        env = dict(os.environ, HOME=str(self.home), USERPROFILE=str(self.home), PYTHONIOENCODING="utf-8")
        res = subprocess.run(
            [sys.executable, str(SCRIPT), "--db", str(self.db_path), *args],
            capture_output=True, env=env, text=True, encoding="utf-8", check=True,
        )
        return res.stdout

    def report(self, *args: str) -> dict:
        data = json.loads(self.run_report("--json", *args))
        data["by_name"] = {c["name"]: c["tokens"] for c in data["categories"]}
        return data

    def basic_steps(self):
        self.db.step(USER_INPUT, payload_field=P_USER, payload=user_input("hello world!!"))  # 13 bytes -> 3
        self.db.step(PLANNER_RESPONSE, metadata=f(9, usage(900, 50)), payload_field=P_PLANNER, payload=planner("r" * 400))
        self.db.step(GENERIC, metadata=tool_call("a" * 40))  # 40 bytes -> 10

    # --- breakdown from the latest generation ---

    def test_exact_breakdown_from_chat_items(self):
        self.basic_steps()
        self.db.generation([
            group(1, 1000, ("skills", 200), ("user_rules", 50), ("subagents", 30), ("flows", 20)),
            group(2, 500),
            group(3, 30),
            group(4, 40),
            group(7, 370, ("0: USER_INPUT", 20), ("1: PLANNER_RESPONSE", 200), ("2: GENERIC", 80),
                  ("3: FINISH", 30), ("4: CHECKPOINT", 40)),
            group(8, 11),
            group(9, 12),
            group(10, 13),
        ])
        r = self.report()
        c = r["by_name"]
        self.assertEqual(c["User messages"], 20)
        self.assertEqual(c["Agent responses"], 240)  # planner + checkpoint
        self.assertEqual(c["Tool calls"], 80)
        self.assertEqual(c["System messages"], 30)  # FINISH
        self.assertEqual(c["System prompt"], 750)  # user_rules stays in the system prompt
        self.assertEqual(c["Skills"], 200)
        self.assertEqual(c["Subagents"], 30)
        self.assertEqual(c["Workflows"], 33)  # flows item + FLOWS group
        self.assertEqual(c["Legacy workflows"], 11)
        self.assertEqual(c["Plugins"], 12)
        self.assertEqual(c["Rules"], 30)
        self.assertEqual(c["MCP tools"], 40)
        self.assertNotIn("System overhead", c)
        self.assertEqual(r["used_tokens"], sum(v for v in c.values()))
        self.assertEqual(r["limit_tokens"], 1_000_000)
        self.assertTrue(r["has_breakdown"])

    def test_latest_generation_wins_over_larger_one(self):
        self.basic_steps()
        self.db.generation([group(1, 90000), group(7, 10, ("0: USER_INPUT", 10))])
        self.db.generation([group(1, 100), group(7, 10, ("0: USER_INPUT", 10))])
        self.assertEqual(self.report()["by_name"]["System prompt"], 100)

    def test_unknown_item_scales_step_estimates(self):
        self.basic_steps()
        self.db.generation([group(7, 315, ("0: USER_INPUT", 20), ("1: NOT_A_STEP_TYPE", 1))])
        c = self.report()["by_name"]
        # Step estimates 3 / 50 / 10 scaled by 315 / 63 = 5.
        self.assertEqual((c["User messages"], c["Agent responses"], c["Tool calls"]), (15, 250, 50))

    def test_overhead_only_without_prompt_or_tools(self):
        self.basic_steps()
        self.db.generation([group(7, 300, ("0: USER_INPUT", 300))], usage=usage(1000, 0, 500))
        self.assertEqual(self.report()["by_name"]["System overhead"], 1200)

    def test_no_overhead_with_system_prompt(self):
        self.basic_steps()
        self.db.generation([group(1, 100), group(7, 300, ("0: USER_INPUT", 300))], usage=usage(1000, 0, 500))
        self.assertNotIn("System overhead", self.report()["by_name"])

    # --- estimates without a breakdown ---

    def test_awaiting_generation_uses_step_estimates(self):
        self.basic_steps()
        self.db.step(FINISH, metadata=tool_call("b" * 8))
        r = self.report()
        c = r["by_name"]
        self.assertFalse(r["has_breakdown"])
        self.assertEqual((c["User messages"], c["Agent responses"], c["Tool calls"], c["System messages"]), (3, 50, 10, 2))
        self.assertEqual(r["limit_tokens"], 0)
        self.assertIn("Estimated usage (awaiting generation)", self.run_report())

    def test_tool_call_output_tokens_take_precedence(self):
        self.db.step(GENERIC, metadata=m(tool_call("a" * 400), f(23, 7)))
        self.assertEqual(self.report()["by_name"]["Tool calls"], 7)

    # --- compaction ---

    def test_compaction_zeroes_summarized_steps(self):
        self.basic_steps()
        self.db.step(CHECKPOINT, payload_field=P_CHECKPOINT,
                     payload=m(f(1, 0), f(11, 0), f(12, 2), f(4, "Fix the login bug"), f(5, "s" * 383)))
        self.db.step(USER_INPUT, payload_field=P_USER, payload=user_input("next question please"))  # 20 -> 5
        self.db.generation([group(1, 800), group(7, 370, ("0: USER_INPUT", 370))])
        r = self.report()
        c = r["by_name"]
        self.assertTrue(r["compacted"])
        self.assertEqual(c["User messages"], 5)
        self.assertEqual(c["Agent responses"], 100)  # (17 + 383) / 4 summary estimate
        self.assertEqual(c["Tool calls"], 0)
        self.assertEqual(c["System prompt"], 800)
        self.assertEqual(r["active_checkpoint"], 0)
        out = self.run_report()
        self.assertIn("Estimated after compaction · updates next turn", out)
        self.assertIn("Checkpoints (1) · /rewind", out)
        self.assertIn("└ Checkpoint 1 (active, in context): Fix the login bug", out)

    def test_checkpoint_already_in_generation_is_not_newer(self):
        self.basic_steps()
        self.db.step(CHECKPOINT, payload_field=P_CHECKPOINT, payload=m(f(1, 0), f(11, 0), f(12, 2)))
        self.db.generation([group(7, 10, ("0: USER_INPUT", 10))], checkpoint_index=0)
        r = self.report()
        self.assertFalse(r["compacted"])
        self.assertIn("steps 0–2", self.run_report())

    def test_checkpoint_buffer_counts_checkpoint_steps(self):
        self.db.step(CHECKPOINT, metadata=tool_call("c" * 80), payload_field=P_CHECKPOINT, payload=f(1, 0))
        self.db.step(PLANNER_RESPONSE, metadata=f(9, usage(0, 5)), payload_field=P_PLANNER, payload=planner("x"))
        r = self.report()
        self.assertEqual(r["checkpoint_buffer"], 20)
        self.assertNotIn(20, [v for k, v in r["by_name"].items()])

    # --- files ---

    def test_file_sections(self):
        home_uri = "file:///" + str(self.home).replace("\\", "/").lstrip("/")
        self.db.step(VIEW_FILE, metadata=tool_call("v" * 40), payload_field=P_VIEW_FILE,
                     payload=m(f(1, home_uri + "/.gemini/skills/foo/SKILL.md"), f(17, 1)))
        edit = lambda path: f(2, f(1, f(8, home_uri + path)))  # noqa: E731
        self.db.step(CODE_ACTION, metadata=tool_call("w" * 80), payload_field=P_CODE_ACTION, payload=m(edit("/brain/task.md"), f(21, 1)))
        self.db.step(CODE_ACTION, metadata=tool_call("w" * 80), payload_field=P_CODE_ACTION, payload=m(edit("/brain/task.md"), f(21, 1)))
        self.db.step(CODE_ACTION, metadata=tool_call("k" * 40), payload_field=P_CODE_ACTION, payload=m(edit("/knowledge/a.md"), f(24, 1)))
        (self.home / ".gemini" / "skills").mkdir(parents=True)
        (self.home / ".gemini" / "GEMINI.md").write_text("x")
        (self.home / ".gemini" / "antigravity" / "skills").mkdir(parents=True)
        (self.home / ".gemini" / "config" / "skills").mkdir(parents=True)  # not an auto-loaded folder
        (self.home / "ws" / ".agents" / "rules").mkdir(parents=True)
        (self.home / "ws" / ".agents" / "hooks.json").write_text("{}")
        r = self.report("--workspace", str(self.home / "ws"))
        self.assertEqual(r["skill_files"], [{"path": "~/.gemini/skills/foo/SKILL.md", "tokens": 10}])
        self.assertEqual(r["artifact_files"], [{"path": "~/brain/task.md", "tokens": 20}])
        self.assertEqual(r["knowledge_files"], [{"path": "~/knowledge/a.md", "tokens": 10}])
        self.assertEqual([s["path"] for s in r["system_files"]],
                         ["~/.gemini/GEMINI.md", "~/.gemini/skills/", "~/.gemini/antigravity/skills/",
                          ".agents/rules/", ".agents/hooks.json"])
        out = self.run_report("--workspace", str(self.home / "ws"))
        self.assertIn("Knowledge files · /rules\n └ ~/knowledge/a.md: 10 tokens", out)
        self.assertIn("System files · auto-loaded\n └ ~/.gemini/GEMINI.md\n", out)

    def test_generic_steps_are_rehydrated(self):
        home_uri = "file:///" + str(self.home).replace("\\", "/").lstrip("/")
        # Desktop app: a write wrapped in a GENERIC step that packs the original code_action step.
        original = m(f(1, CODE_ACTION), f(4, 3), f(5, tool_call("w" * 80)),
                     f(P_CODE_ACTION, m(f(2, f(1, f(8, home_uri + "/brain/plan.md"))), f(21, 1))))
        packed = m(f(1, "type.googleapis.com/gemini_coder.Step"), f(2, original))
        self.db.step(GENERIC, metadata=tool_call("ignored"), payload_field=140, payload=f(2, f(6, packed)))
        # A GENERIC view_file with no packed original: rebuilt from the tool call arguments.
        args = json.dumps({"AbsolutePath": str(self.home / "skills" / "x" / "SKILL.md")})
        self.db.step(GENERIC, metadata=f(4, m(f(2, "view_file"), f(3, args))), payload_field=140, payload=f(2, b""))
        r = self.report()
        self.assertEqual(r["artifact_files"], [{"path": "~/brain/plan.md", "tokens": 20}])
        self.assertEqual(r["skill_files"], [{"path": "~/skills/x/SKILL.md", "tokens": len(args) // 4}])

    # --- model label and context limit ---

    def test_native_window_and_label_from_model_id(self):
        self.basic_steps()
        self.db.generation([group(7, 300, ("0: USER_INPUT", 300))], max_context=256_000,
                           model="gemini-3.8-flash-medium", display_name="")
        r = self.report()
        self.assertEqual(r["model"], "Gemini 3.8 Flash (Medium)")
        self.assertEqual(r["limit_tokens"], 1_048_576)
        self.assertEqual(r["request_budget_tokens"], 256_000)
        self.assertIn("Gemini 3.8 Flash (Medium) · 300/1.0M tokens", self.run_report())

    def test_model_catalog_overrides_table(self):
        self.basic_steps()
        self.db.generation([group(1, 100), group(7, 300, ("0: USER_INPUT", 300))],
                           model="gemini-9-test-high", display_name="")
        catalog = self.home / "catalog.json"
        catalog.write_text(json.dumps({"gemini-9-test-high": {"maxTokens": 2_000_000, "displayName": "Gemini 9 Test (High)"}}))
        r = self.report("--model-catalog", str(catalog))
        self.assertEqual((r["model"], r["limit_tokens"]), ("Gemini 9 Test (High)", 2_000_000))
        self.assertEqual(r["heading"], "Token usage by category")
        self.assertEqual(r["grid_segments"], [100, 300, 0, 0, 0])
        self.assertTrue(r["text"].startswith("└ Context Usage"))
        self.assertIn("Gemini 9 Test (High) · 400/2M tokens", r["text"])

    def test_unknown_model_falls_back_to_request_budget(self):
        self.basic_steps()
        self.db.generation([group(7, 300, ("0: USER_INPUT", 300))], max_context=256_000, model="new-model-x", display_name="")
        r = self.report()
        self.assertEqual((r["model"], r["limit_tokens"]), ("New Model X", 256_000))

    # --- rendering ---

    def test_text_layout(self):
        self.basic_steps()
        self.db.generation([group(1, 1000), group(7, 300, ("0: USER_INPUT", 300))], max_context=256_000)
        out = self.run_report()
        lines = out.splitlines()
        self.assertEqual(lines[0], "└ Context Usage")
        self.assertTrue(lines[2].endswith("Test Model · 1.3k/256.0k tokens"))
        self.assertIn("◉ Agent responses: 0 tokens (0.0%)", out)  # always shown
        self.assertNotIn("Plugins", out)  # zero, hidden
        self.assertTrue(out.rstrip().endswith("Related: /artifact · /skill · /rewind"))
        grid_rows = [l for l in lines if l.startswith(("◉", "□"))]
        self.assertEqual(len(grid_rows[0].split("   ")[0].split(" ")), 20)  # 20 columns at width 100

    def test_limit_override_and_narrow_layout(self):
        self.basic_steps()
        self.db.generation([group(7, 300, ("0: USER_INPUT", 300))])
        out = self.run_report("--limit", "200000", "--width", "60")
        lines = out.splitlines()
        self.assertEqual(lines[2], "Test Model · 300/200.0k tokens")  # stats above the grid
        self.assertTrue(lines[-3].startswith("□"))


class FormatTest(unittest.TestCase):
    def test_model_label(self):
        cases = {
            "gemini-4-argon-medium": "Gemini 4 Argon (Medium)",
            "gemini-3.8-flash-low": "Gemini 3.8 Flash (Low)",
            "claude-opus-5-5-high": "Claude Opus 5.5 (High)",
            "gpt-oss-120b-medium": "GPT-OSS 120B (Medium)",
        }
        for slug, label in cases.items():
            self.assertEqual(context_report.model_label(slug), label)

    def test_native_windows(self):
        for slug, tokens in {"claude-sonnet-5-5-low": 1_000_000, "claude-opus-4-6-thinking": 250_000,
                             "gpt-oss-120b-medium": 131_072, "gemini-4-argon-high": 1_048_576}.items():
            gen = context_report.Msg(f(3, f(28, slug)))
            self.assertEqual(context_report.context_limit(gen), tokens)

    def test_fmt_tok(self):
        self.assertEqual(context_report.fmt_tok(999), "999")
        self.assertEqual(context_report.fmt_tok(1500), "1.5k")
        self.assertEqual(context_report.fmt_tok(1_000_000), "1M")
        self.assertEqual(context_report.fmt_tok(1_250_000), "1.2M")


if __name__ == "__main__":
    unittest.main()
