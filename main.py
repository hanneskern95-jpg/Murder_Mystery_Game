"""Console entry point for the full-context Murder Mystery Game.

The narrator deliberately receives the complete case on every turn.  This module
does not hide parts of the case behind lookup tools: the agents' instructions
govern what may be said to the player, while Python records game state.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agents import Agent, Runner, function_tool
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent
DEBUG = True
load_dotenv(ROOT / ".env")
DESIGNER_MODEL = os.getenv("CASE_DESIGNER_MODEL", "gpt-6-sol")
NARRATOR_MODEL = os.getenv("NARRATOR_MODEL", "gpt-6-luna")
RECENT_ROUNDS = 10


def debug_tool_call(tool_name: str) -> None:
    if DEBUG:
        print(f"calling {tool_name}")


def read_instructions(name: str) -> str:
    return (ROOT / "agents" / name).read_text(encoding="utf-8")


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def validate_case(case: Any) -> list[str]:
    """Validate cross references and the minimum playable case structure."""
    if not isinstance(case, dict):
        return ["The case file must be one JSON object."]
    required = {
        "overview", "player_role", "private_solution", "timeline", "characters",
        "clues", "mysteries", "opening", "consistency_check",
    }
    errors = [f"Missing top-level section: {key}" for key in sorted(required - case.keys())]
    if errors:
        return errors
    for key in ("characters", "clues", "mysteries", "timeline"):
        if not isinstance(case[key], list):
            errors.append(f"{key} must be an array.")
    if errors:
        return errors

    clues = case["clues"]
    mysteries = case["mysteries"]
    characters = case["characters"]
    clue_ids = [item.get("id") for item in clues if isinstance(item, dict)]
    mystery_ids = [item.get("id") for item in mysteries if isinstance(item, dict)]
    if not all(isinstance(item, str) and item for item in clue_ids) or len(clue_ids) != len(set(clue_ids)):
        errors.append("Clue IDs must be non-empty and unique.")
    if not all(isinstance(item, str) and item for item in mystery_ids) or len(mystery_ids) != len(set(mystery_ids)):
        errors.append("Mystery IDs must be non-empty and unique.")
    murder = [m for m in mysteries if isinstance(m, dict) and m.get("id") == "murder"]
    if len(murder) != 1 or murder[0].get("type") != "murder" or murder[0].get("scale") != 5:
        errors.append("There must be exactly one scale-5 murder mystery with id 'murder'.")
    elif not 1 <= len(murder[0].get("required_mystery_ids", [])) <= 2:
        errors.append("The murder needs one or two required auxiliary mystery IDs.")
    auxiliaries = [m for m in mysteries if isinstance(m, dict) and m.get("id") != "murder"]
    if len(auxiliaries) < 3:
        errors.append("Include at least three auxiliary mysteries.")
    if len([c for c in characters if isinstance(c, dict) and c.get("importance") == "major"]) < 3:
        errors.append("Include at least three major characters.")
    if len([c for c in characters if isinstance(c, dict) and c.get("is_suspect")]) < 2:
        errors.append("Include at least two suspects.")
    for clue in clues:
        if not isinstance(clue, dict):
            continue
        for key in ("id", "description", "what_it_proves"):
            if key not in clue:
                errors.append(f"Clue {clue.get('id', '<unknown>')} is missing {key}.")
    for mystery in mysteries:
        if not isinstance(mystery, dict):
            continue
        for clue_id in mystery.get("clue_ids", []):
            if clue_id not in clue_ids:
                errors.append(f"Mystery {mystery.get('id')} references unknown clue {clue_id}.")
        for mystery_id in mystery.get("required_mystery_ids", []):
            if mystery_id not in mystery_ids or mystery_id == "murder":
                errors.append(f"Mystery {mystery.get('id')} references invalid prerequisite {mystery_id}.")
    return errors


@dataclass
class GameState:
    case_file: dict[str, Any]
    discovered_clues: list[str] = field(default_factory=list)
    resolved_mysteries: list[str] = field(default_factory=list)
    disclosed_information: list[str] = field(default_factory=list)
    revealed_objects: list[str] = field(default_factory=list)
    revealed_environmental_secrets: list[str] = field(default_factory=list)
    player_facts: list[str] = field(default_factory=list)
    added_details: list[dict[str, Any]] = field(default_factory=list)
    emergency_clues: list[dict[str, Any]] = field(default_factory=list)
    character_knowledge: list[dict[str, Any]] = field(default_factory=list)
    rounds: list[dict[str, str]] = field(default_factory=list)

    def context(self) -> dict[str, Any]:
        return {
            "complete_case_file": self.case_file,
            "last_10_rounds": self.rounds[-RECENT_ROUNDS:],
            "player_state": {
                "discovered_clue_ids": self.discovered_clues,
                "resolved_mystery_ids": self.resolved_mysteries,
                "disclosed_guarded_information_ids": self.disclosed_information,
                "revealed_object_ids": self.revealed_objects,
                "revealed_environmental_secret_ids": self.revealed_environmental_secrets,
                "established_facts": self.player_facts,
            },
            "details_added_during_play": self.added_details,
            "emergency_clues_added_during_play": self.emergency_clues,
            "character_knowledge_added_during_play": self.character_knowledge,
        }


def make_designer() -> tuple[Agent, dict[str, Any]]:
    saved: dict[str, Any] = {}

    # A case file is intentionally an open-ended JSON object. The SDK's strict
    # tool schemas reject dict[str, Any] because it maps to additionalProperties.
    @function_tool(strict_mode=False)
    def save_case_file(case_file: dict[str, Any]) -> str:
        """Save the complete canonical case file after validation."""
        debug_tool_call("save_case_file")
        errors = validate_case(case_file)
        if errors:
            return "Case not saved. Fix these validation errors:\n- " + "\n- ".join(errors)
        saved["case"] = case_file
        return "Case saved and validated. Do not reveal its private solution in your final response."

    return Agent(
        name="Case Designer",
        model=DESIGNER_MODEL,
        instructions=read_instructions("case_designer.md"),
        tools=[save_case_file],
    ), saved


def designer_consultation(state: GameState, request: str, expected: str) -> dict[str, Any]:
    """Ask the designer for a narrowly scoped, parseable play-time addition."""
    prompt = (
        "You are consulting on an already fixed case. Return ONLY one JSON object, with no markdown. "
        f"The requested addition type is {expected}. Reply {{\"approved\": false, \"reason\": ...}} "
        "if it would conflict with canon or spoil the solution. Otherwise reply "
        "{\"approved\": true, \"addition\": {...}}.\n\n"
        f"REQUEST:\n{request}\n\nFULL CURRENT CONTEXT:\n{compact_json(state.context())}"
    )
    advisor = Agent(name="Case Designer Consultation", model=DESIGNER_MODEL,
                    instructions=read_instructions("case_designer.md"))
    raw = str(Runner.run_sync(advisor, prompt, max_turns=4).final_output).strip()
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        return {"approved": False, "reason": "The designer could not provide a safe canonical ruling."}
    return result if isinstance(result, dict) else {"approved": False, "reason": "Invalid designer ruling."}


def make_narrator(state: GameState) -> Agent:
    clue_index = {c.get("id"): c for c in state.case_file.get("clues", []) if isinstance(c, dict)}
    mystery_index = {m.get("id"): m for m in state.case_file.get("mysteries", []) if isinstance(m, dict)}
    object_index = {o.get("id"): o for o in state.case_file.get("objects", []) if isinstance(o, dict)}
    environmental_secret_index = {
        (place.get("id"), secret.get("id")): secret
        for place in state.case_file.get("places", []) if isinstance(place, dict)
        for secret in place.get("secrets", []) if isinstance(secret, dict)
    }
    guarded_index = {
        item.get("id"): item
        for character in state.case_file.get("characters", []) if isinstance(character, dict)
        for item in character.get("guarded_information", []) if isinstance(item, dict)
    }

    @function_tool
    def record_discovered_clue(clue_id: str, note: str) -> str:
        """Record a clue the player has just legitimately discovered; returns its player-facing description."""
        debug_tool_call("record_discovered_clue")
        clue = clue_index.get(clue_id)
        if not clue:
            return "Unknown clue ID; do not narrate it."
        if clue_id not in state.discovered_clues:
            state.discovered_clues.append(clue_id)
        return f"Recorded. Player-facing description: {clue.get('description', '')}"

    @function_tool
    def record_revealed_object(object_id: str, note: str) -> str:
        """Record a hidden or newly exposed object the player has fairly found; returns its description."""
        debug_tool_call("record_revealed_object")
        item = object_index.get(object_id)
        if not item:
            return "Unknown object ID; do not reveal it."
        if object_id not in state.revealed_objects:
            state.revealed_objects.append(object_id)
        return f"Recorded. Object now available for narration: {item.get('name', '')}: {item.get('description', '')}"

    @function_tool
    def record_revealed_environmental_secret(place_id: str, secret_id: str, note: str) -> str:
        """Record an environmental secret fairly uncovered at a place; returns its description."""
        debug_tool_call("record_revealed_environmental_secret")
        key = (place_id, secret_id)
        item = environmental_secret_index.get(key)
        if not item:
            return "Unknown environmental secret; do not reveal it."
        record_id = f"{place_id}:{secret_id}"
        if record_id not in state.revealed_environmental_secrets:
            state.revealed_environmental_secrets.append(record_id)
        return f"Recorded. Environmental secret now available for narration: {item.get('description', '')}"

    @function_tool
    def record_resolved_mystery(mystery_id: str, note: str) -> str:
        """Record a mystery the player has genuinely resolved; returns its private answer for narration."""
        debug_tool_call("record_resolved_mystery")
        mystery = mystery_index.get(mystery_id)
        if not mystery:
            return "Unknown mystery ID; do not resolve it."
        if mystery_id not in state.resolved_mysteries:
            state.resolved_mysteries.append(mystery_id)
        return f"Recorded. Answer now available for narration: {mystery.get('answer', '')}"

    @function_tool
    def record_guarded_information(information_id: str, note: str) -> str:
        """Record guarded character information that has fairly become known; returns its content."""
        debug_tool_call("record_guarded_information")
        item = guarded_index.get(information_id)
        if not item:
            return "Unknown guarded-information ID; do not narrate it."
        if information_id not in state.disclosed_information:
            state.disclosed_information.append(information_id)
        return f"Recorded. Content now available for narration: {item.get('content', '')}"

    @function_tool
    def record_player_fact(fact: str) -> str:
        """Record a concise objective fact that has been stated to the player this turn."""
        debug_tool_call("record_player_fact")
        if fact not in state.player_facts:
            state.player_facts.append(fact)
        return "Fact recorded."

    @function_tool
    def request_case_detail(request: str) -> str:
        """Request one small canon-compatible detail for an intentionally open gap."""
        debug_tool_call("request_case_detail")
        ruling = designer_consultation(state, request, "case_detail")
        if ruling.get("approved") and isinstance(ruling.get("addition"), dict):
            state.added_details.append(ruling["addition"])
            return "Approved detail: " + compact_json(ruling["addition"])
        return "Not approved: " + str(ruling.get("reason", "No compatible detail."))

    @function_tool
    def request_character_knowledge(request: str) -> str:
        """Request one vague, spoiler-free lead a plausible character could provide."""
        debug_tool_call("request_character_knowledge")
        ruling = designer_consultation(state, request, "character_knowledge")
        if ruling.get("approved") and isinstance(ruling.get("addition"), dict):
            state.character_knowledge.append(ruling["addition"])
            return "Approved character lead: " + compact_json(ruling["addition"])
        return "Not approved: " + str(ruling.get("reason", "Use an existing lead instead."))

    @function_tool
    def request_emergency_clue(request: str) -> str:
        """Request one last-resort, canon-compatible clue for a specifically blocked fair path."""
        debug_tool_call("request_emergency_clue")
        ruling = designer_consultation(state, request, "emergency_clue")
        if ruling.get("approved") and isinstance(ruling.get("addition"), dict):
            clue = ruling["addition"]
            if not clue.get("id") or clue.get("id") in clue_index:
                return "Not approved: emergency clue needs a new unique ID."
            state.emergency_clues.append(clue)
            state.case_file.setdefault("clues", []).append(clue)
            clue_index[clue["id"]] = clue
            return "Approved emergency clue. Nudge its general location; do not reveal it until investigated."
        return "Not approved: " + str(ruling.get("reason", "Existing paths remain fair."))

    full_context = compact_json(state.context())
    instructions = read_instructions("narrator.md") + "\n\nCURRENT FULL GAME CONTEXT:\n" + full_context
    return Agent(
        name="Narrator", model=NARRATOR_MODEL, instructions=instructions,
        tools=[record_discovered_clue, record_revealed_object,
               record_revealed_environmental_secret, record_resolved_mystery, record_guarded_information,
               record_player_fact, request_case_detail, request_character_knowledge,
               request_emergency_clue],
    )


def run() -> None:
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is missing. Add it to .env beside main.py.")
    print("Murder Mystery Game\n")
    premise = input("Please choose a setting or scenario to play.\n\n").strip()
    if not premise:
        premise = "Create a classic fair-play murder mystery with an atmospheric setting."

    designer, saved = make_designer()
    design_prompt = (
        "Create a complete playable case for this player premise. Call save_case_file with the "
        "entire JSON object. If validation reports errors, repair it and call the tool again.\n\n"
        f"PLAYER PREMISE:\n{premise}"
    )
    print("\nThe case is being prepared …")
    Runner.run_sync(designer, design_prompt, max_turns=12)
    if "case" not in saved:
        raise RuntimeError("The Case Designer did not save a valid case. Please run again.")

    if DEBUG:
        case_path = ROOT / "case_file.json"
        case_path.write_text(compact_json(saved["case"]), encoding="utf-8")
        print(f"Debug case file saved to {case_path}")

    state = GameState(case_file=saved["case"])
    opening = str(state.case_file["opening"])
    print("\n" + opening + "\n\nWhat do you want to do?")
    state.rounds.append({"player": "[Spielbeginn]", "narrator": opening})

    while True:
        action = input("\n> ").strip()
        if action.lower() in {"/quit", "/exit", "quit", "exit"}:
            print("Thank you for playing.")
            return
        if not action:
            continue
        narrator = make_narrator(state)
        result = Runner.run_sync(narrator, action, max_turns=10)
        response = str(result.final_output)
        print("\n" + response)
        state.rounds.append({"player": action, "narrator": response})


if __name__ == "__main__":
    run()
