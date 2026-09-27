"""agent-eval runner, planning runs: the parts whose defects corrupt a conclusion silently.

Token attribution, list-price cost, the isolation check and run validity produce no
error when they are wrong; they produce a plausible number or a passing check. Each
test here names the wrong number or the missed leak it would let through. The isolation
cases that scan a run root with grep are in tests/integration/test_evalkit_planning_integration.py.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
KIT_PATH = REPO_ROOT / "skills" / "engineering" / "agent-eval" / "scripts" / "evalkit.py"


@pytest.fixture(scope="module")
def kit():
    spec = importlib.util.spec_from_file_location("evalkit_planning_under_test", KIT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _assistant(mid, model, output, input_=10, cache_read=0, w5=0, w1=0, effort=None):
    row = {"type": "assistant", "message": {"role": "assistant", "id": mid, "model": model, "usage": {
        "input_tokens": input_, "output_tokens": output, "cache_read_input_tokens": cache_read,
        "cache_creation": {"ephemeral_5m_input_tokens": w5, "ephemeral_1h_input_tokens": w1}}}}
    if effort:
        row["effort"] = effort
    return row


# ------------------------------------------------------------ token attribution

def test_a_message_split_across_lines_is_counted_once_at_its_final_usage(kit):
    # Claude writes one line per content block; the streamed copy carries output 0.
    rows = [_assistant("m1", "claude-opus-5-5", 0, effort="high"),
            _assistant("m1", "claude-opus-5-5", 900, effort="high")]
    usage, efforts = kit.claude_transcript_usage(rows)
    assert usage["claude-opus-5-5"]["output"] == 900
    assert usage["claude-opus-5-5"]["input"] == 10
    assert usage["claude-opus-5-5"]["messages"] == 1
    assert efforts == ["high"]


def test_two_models_in_one_session_are_attributed_separately(kit):
    rows = [_assistant("a", "claude-fable-5-1", 100, cache_read=50, w5=20),
            _assistant("b", "claude-opus-5-5", 40, w1=5)]
    usage, _ = kit.claude_transcript_usage(rows)
    assert set(usage) == {"claude-fable-5-1", "claude-opus-5-5"}
    assert usage["claude-fable-5-1"] == {"input": 10, "output": 100, "cache_read": 50, "cache_write_5m": 20,
                                         "cache_write_1h": 0, "messages": 1}
    assert usage["claude-opus-5-5"]["cache_write_1h"] == 5


def test_user_rows_and_tool_results_carry_no_usage(kit):
    rows = [{"type": "user", "message": {"role": "user", "content": "hi"}},
            _assistant("a", "claude-opus-5-5", 7)]
    usage, _ = kit.claude_transcript_usage(rows)
    assert usage["claude-opus-5-5"]["messages"] == 1


def test_codex_usage_is_the_last_cumulative_total_not_a_sum_of_turns(kit):
    # After `exec resume` the second total already includes the first turn.
    def count(inp, cached, out):
        return {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {
            "input_tokens": inp, "cached_input_tokens": cached, "cache_write_input_tokens": 0,
            "output_tokens": out, "reasoning_output_tokens": 1}}}}
    rows = [{"type": "turn_context", "payload": {"model": "gpt-6-luna", "effort": "max"}},
            count(14625, 2816, 5),
            {"type": "turn_context", "payload": {"model": "gpt-6-luna", "effort": "max"}},
            count(29266, 16896, 10)]
    usage, models, efforts = kit.codex_rollout_usage(rows)
    assert models == ["gpt-6-luna"] and efforts == ["max"]
    assert usage["gpt-6-luna"]["input"] == 29266 - 16896
    assert usage["gpt-6-luna"]["cache_read"] == 16896
    assert usage["gpt-6-luna"]["output"] == 10


def test_a_rollout_without_token_counts_reports_no_usage(kit):
    usage, models, _ = kit.codex_rollout_usage([{"type": "turn_context", "payload": {"model": "gpt-6-sol"}}])
    assert usage == {} and models == ["gpt-6-sol"]


def test_two_effort_values_in_one_session_are_both_reported(kit):
    rows = [{"type": "turn_context", "payload": {"model": "gpt-6-sol", "effort": "high"}},
            {"type": "turn_context", "payload": {"model": "gpt-6-sol", "effort": "ultra"}}]
    _, _, efforts = kit.codex_rollout_usage(rows)
    assert efforts == ["high", "ultra"]


def _rollout(path, meta, model, effort, inp, cached, out):
    rows = [{"type": "session_meta", "payload": meta},
            {"type": "turn_context", "payload": {"model": model, "effort": effort}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {
                "input_tokens": inp, "cached_input_tokens": cached, "cache_write_input_tokens": 0,
                "output_tokens": out, "reasoning_output_tokens": 0}}}}]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


def test_subagent_rollouts_are_billed_but_do_not_decide_the_effort(kit, tmp_path):
    # A candidate spawned a subagent at a lower effort with its own rollout; reading only
    # the last file under-costed the run and flagged a false effort mismatch.
    main = _rollout(tmp_path / "rollout-a.jsonl", {"id": "p", "thread_source": "user"},
                    "gpt-6-astra", "ultra", 2_000_000, 1_900_000, 17_000)
    sub = _rollout(tmp_path / "rollout-b.jsonl", {"id": "c", "thread_source": "subagent", "parent_thread_id": "p"},
                   "gpt-6-astra", "xhigh", 1_500_000, 1_400_000, 8_000)
    usage, models, efforts, subagents = kit.codex_usage_from_files([str(sub), str(main)])
    assert efforts == ["ultra"] and models == ["gpt-6-astra"]
    assert usage["gpt-6-astra"]["cache_read"] == 1_900_000 + 1_400_000
    assert usage["gpt-6-astra"]["input"] == 100_000 + 100_000
    assert usage["gpt-6-astra"]["output"] == 25_000
    assert subagents == [{"id": "c", "role": None, "models": ["gpt-6-astra"], "efforts": ["xhigh"]}]


def test_a_single_rollout_session_is_unchanged_by_per_file_aggregation(kit, tmp_path):
    only = _rollout(tmp_path / "rollout-a.jsonl", {"id": "p"}, "gpt-6-sol", "high", 1000, 400, 50)
    usage, models, efforts, subagents = kit.codex_usage_from_files([str(only)])
    assert usage == {"gpt-6-sol": {"input": 600, "cache_read": 400, "cache_write": 0, "output": 50, "reasoning": 0}}
    assert (models, efforts, subagents) == (["gpt-6-sol"], ["high"], [])


# --------------------------------------------------------------------- cost

PRICES = {
    "claude-opus-5-5": {"input": 4.0, "output": 20.0, "cache_read": 0.2, "cache_write_5m": 5.0, "cache_write_1h": 8.0},
    "gpt-6-sol": {"input": 2.0, "output": 10.0, "cache_read": 0.2, "cache_write": 2.5},
    "deepseek-flash": {"input": 0.3, "output": 1.2, "cache_read": 0.006},
}


def test_claude_cost_prices_every_cache_leg(kit):
    usage = {"claude-opus-5-5": {"input": 1_000_000, "output": 1_000_000, "cache_read": 1_000_000,
                                 "cache_write_5m": 1_000_000, "cache_write_1h": 1_000_000}}
    assert kit.cost_usd(usage, PRICES) == pytest.approx(4 + 20 + 0.2 + 5 + 8)


def test_codex_cost_prices_cached_input_at_the_cached_rate(kit):
    usage = {"gpt-6-sol": {"input": 500_000, "cache_read": 500_000, "cache_write": 0, "output": 100_000}}
    assert kit.cost_usd(usage, PRICES) == pytest.approx(1.0 + 0.1 + 1.0)


def test_a_long_context_suffix_prices_as_the_base_model(kit):
    assert kit.cost_usd({"deepseek-flash[1m]": {"input": 1_000_000}}, PRICES) == pytest.approx(0.3)


def test_an_unpriced_model_makes_the_cost_unknown_not_zero(kit):
    usage = {"gpt-6-sol": {"input": 1_000_000}, "mystery-model": {"input": 1}}
    assert kit.cost_usd(usage, PRICES) is None


# ---------------------------------------------------------------- isolation

def _root(tmp_path):
    for sub in ("wt", "home", "tmp"):
        (tmp_path / sub).mkdir()
    (tmp_path / "wt" / "AGENTS.md").write_text("# entry\n")
    return tmp_path


@pytest.mark.parametrize("key", ["GH_TOKEN", "GITHUB_TOKEN", "OP_SERVICE_ACCOUNT_TOKEN", "AWS_PROFILE",
                                 "SSH_AUTH_SOCK", "ANTHROPIC_API_KEY"])
def test_a_forge_vault_or_cloud_credential_in_the_environment_fails(kit, tmp_path, key):
    root = _root(tmp_path)
    failures = kit.isolation_check(root, [], "p", {key: "x"}, "claude", "native", argv=kit.claude_argv("m", "high"))
    assert any(key in f for f in failures)


def test_a_native_claude_candidate_must_not_inherit_the_gateway(kit):
    assert kit.forbidden_env({"ANTHROPIC_BASE_URL": "x"}, "claude", "native") == ["ANTHROPIC_BASE_URL"]
    assert kit.forbidden_env({"ANTHROPIC_BASE_URL": "x", "ANTHROPIC_AUTH_TOKEN": "y"}, "claude", "gateway") == []


@pytest.mark.parametrize("drop", ["--restricted", "--strict-mcp-config"])
def test_a_claude_launch_without_its_confinement_flags_fails(kit, tmp_path, drop):
    argv = [a for a in kit.claude_argv("m", "high") if a != drop]
    failures = kit.isolation_check(_root(tmp_path), [], "p", {}, "claude", "native", argv=argv)
    assert any(drop in f for f in failures)


def test_a_claude_launch_with_a_shell_tool_fails(kit, tmp_path):
    argv = kit.claude_argv("m", "high", tools="Read,Glob,Grep,Bash")
    failures = kit.isolation_check(_root(tmp_path), [], "p", {}, "claude", "native", argv=argv)
    assert any("tools" in f for f in failures)


def test_the_candidate_builders_never_hand_over_a_credential(kit, tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "secret")
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "secret")
    assert kit.forbidden_env(kit.claude_env("native", "k"), "claude", "native") == []
    assert kit.forbidden_env(kit.codex_env(tmp_path, tmp_path / "c", "k"), "codex", "gateway") == []


def test_a_codex_run_without_a_sandbox_profile_fails(kit, tmp_path):
    failures = kit.isolation_check(_root(tmp_path), [], "p", {}, "codex", "gateway", profile_path=None)
    assert any("sandbox profile" in f for f in failures)


def test_the_sandbox_profile_denies_user_data_and_other_temp_trees_but_not_the_run(kit, tmp_path):
    profile = kit.sandbox_profile(tmp_path)
    deny = re.search(r'\(deny file-read-data file-write\*([^\n]*)\)', profile).group(1)
    for tree in ("/Users", "/Volumes", "/private/tmp"):
        assert f'(subpath "{tree}")' in deny
    allow = profile.index(f'(allow file-read-data file-write* (subpath "{Path(tmp_path).resolve()}"))')
    assert allow > profile.index("(deny file-read-data"), "the run root must be allowed after the broad deny"


# ------------------------------------------------------------------- validity

def _record(**over):
    rec = {"stages": [{"stage": "draft", "returncode": 0, "timed_out": False, "final": "plan",
                       "init_tools": ["Glob", "Grep", "Read"], "init_mcp": []}],
           "usage": {"claude-opus-5-5": {"input": 1}}, "usage_found": True,
           "actual_models": ["claude-opus-5-5"], "actual_efforts": ["high"], "cost_usd": 0.1}
    rec.update(over)
    return rec


CAND = {"model": "claude-opus-5-5", "runtime": "claude", "client": "native"}


def test_a_complete_run_is_valid(kit):
    assert kit.validity(_record(), CAND, "high") == []


@pytest.mark.parametrize("over,reason", [
    ({"actual_efforts": ["medium"]}, "effort mismatch"),
    ({"actual_models": ["claude-opus-5"]}, "model mismatch"),
    ({"usage": {}}, "no usage"),
    ({"cost_usd": None}, "cost unknown"),
])
def test_an_unproven_run_is_invalid_not_cheap(kit, over, reason):
    assert any(reason in r for r in kit.validity(_record(**over), CAND, "high"))


def test_a_timed_out_or_empty_stage_is_invalid(kit):
    rec = _record(stages=[{"stage": "draft", "returncode": -9, "timed_out": True, "final": ""}])
    reasons = kit.validity(rec, CAND, "high")
    assert any("timed out" in r for r in reasons) and any("empty answer" in r for r in reasons)


def test_a_claude_stage_that_loaded_more_tools_is_invalid(kit):
    rec = _record(stages=[{"stage": "draft", "returncode": 0, "final": "x",
                           "init_tools": ["Bash", "Glob", "Grep", "Read"], "init_mcp": []}])
    assert any("tools were" in r for r in kit.validity(rec, CAND, "high"))


# ------------------------------------------------ runs, two-level trap, forced adoption

def test_expand_runs_multiplies_cases_efforts_and_modes(kit):
    arms = {"candidates": [{"id": "x", "model": "m", "runtime": "codex", "client": "gateway",
                            "efforts": ["medium", "high"]}], "modes": ["solo", "review"]}
    runs = kit.expand_runs({"I": {}, "A": {}}, arms)
    assert len(runs) == 2 * 2 * 2
    assert {r["run_id"] for r in runs} >= {"I-x-medium-solo", "A-x-high-review"}


def test_the_judge_is_asked_for_both_trap_levels(kit):
    trap = kit.JUDGE_SCHEMA["properties"]["trap"]
    assert set(trap["required"]) == {"direction", "pass", "evidence"}
    case = {"trap": {"description": "waited", "direction": "act before the window", "pass": "acted and verified"},
            "rubric": [{"id": "r1", "text": "names the evidence", "evidence": "answer"}], "equivalents": "none"}
    prompt = kit.judge_prompt(case, {"rubric": []}, "p", [], "answer")
    assert "act before the window" in prompt and "acted and verified" in prompt


def _review_record(run_id, case, cand, effort, status="valid", draft="d", review="r"):
    return {"run_id": run_id, "case": case, "candidate": cand, "effort": effort, "mode": "review",
            "status": status, "draft": draft,
            "stages": [{"stage": "draft"}, {"stage": "review", "final": review}, {"stage": "revise"}]}


def test_adoption_reuses_each_valid_review_once_per_repeat(kit):
    src = [_review_record("I-sol-high-review", "I", "sol", "high", draft="plan-A", review="fix X"),
           _review_record("I-sol-max-review", "I", "sol", "max"),
           _review_record("A-luna-high-review", "A", "luna", "high", status="invalid"),
           _review_record("H-deepseek-high-review", "H", "deepseek", "high", review="  "),
           {"run_id": "I-sol-high-solo", "case": "I", "candidate": "sol", "effort": "high", "mode": "solo",
            "status": "valid", "stages": []}]
    runs = kit.adopt_runs(src, None, ["high"], 2)
    assert [r["run_id"] for r in runs] == ["I-sol-high-adopt-r1", "I-sol-high-adopt-r2"]
    assert all(r["source_run"] == "I-sol-high-review" and r["mode"] == "adopt" for r in runs)
    assert "plan-A" in runs[0]["prompt_extra"] and "fix X" in runs[0]["prompt_extra"]
    assert "(file and line)" in runs[0]["prompt_extra"]


def test_an_adoption_run_is_judged_on_its_answer_alone(kit):
    assert kit.judge_targets({"mode": "adopt", "final": "x"}) == [("final", "x")]
