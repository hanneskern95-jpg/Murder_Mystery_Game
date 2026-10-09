"""Console murder-mystery game powered by the OpenAI Agents SDK."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from agents import Agent, Runner, function_tool
    from dotenv import load_dotenv
except ImportError as exc:
    raise SystemExit(
        "The OpenAI Agents SDK or python-dotenv is not installed. Run: python -m pip install -r requirements.txt"
    ) from exc


ROOT = Path(__file__).resolve().parent
AGENTS_DIR = ROOT / "agents"
MAX_RECENT_TURNS = 8
DEBUG_MODE = True

load_dotenv(ROOT / ".env")


def read_agent_prompt(filename: str) -> str:
    path = AGENTS_DIR / filename
    if not path.is_file():
        raise FileNotFoundError(f"Agent instructions not found: {path}")
    return path.read_text(encoding="utf-8")


@dataclass
class GameState:
    """All game data lives in memory for this first console version."""

    case_file: dict[str, Any] | None = None
    rejected_case_draft: str = ""
    case_validation_errors: list[str] = field(default_factory=list)
    case_additions: list[dict[str, str]] = field(default_factory=list)
    known_clues: dict[str, str] = field(default_factory=dict)
    resolved_mysteries: dict[str, str] = field(default_factory=dict)
    known_character_information: dict[str, str] = field(default_factory=dict)
    known_facts: list[str] = field(default_factory=list)
    staged_clues: dict[str, str] = field(default_factory=dict)
    staged_mysteries: dict[str, str] = field(default_factory=dict)
    staged_character_information: dict[str, str] = field(default_factory=dict)
    staged_facts: list[str] = field(default_factory=list)
    recent_turns: list[tuple[str, str]] = field(default_factory=list)
    current_player_action: str = ""
    game_started: bool = False
    emergency_clue_request_active: bool = False
    emergency_clue_added_this_request: bool = False
    emergency_clue_target_mysteries: set[str] = field(default_factory=set)
    character_knowledge_request_active: bool = False
    character_knowledge_added_this_request: bool = False
    character_knowledge_request_target: tuple[str, str, str, str] | None = None

    def stage_clue(self, clue_id: str, action_sufficient: bool, action_justification: str) -> str:
        assert self.case_file is not None
        for clue in self.case_file["clues"]:
            if str(clue.get("id", "")).casefold() == clue_id.casefold():
                requirement = str(clue.get("discovery_requirement", "focused investigation"))
                if not action_sufficient:
                    return (
                        "The player's action and known information are not yet sufficient for this clue. "
                        "No clue content was returned. The clue's discovery requirement is: " + requirement
                    )
                observation = {
                    "id": clue["id"],
                    "name": clue["name"],
                    "observation": clue["observation"],
                }
                player_facing = json.dumps(observation, ensure_ascii=False)
                self.staged_clues[str(clue["id"])] = player_facing
                return player_facing
        return "No clue with that ID exists in this case. Do not claim it was discovered."

    def stage_mystery_resolution(self, mystery_id: str, resolution_sufficient: bool, reasoning: str) -> str:
        assert self.case_file is not None
        for mystery in self.case_file["mysteries"]:
            if str(mystery.get("id", "")).casefold() != mystery_id.casefold():
                continue
            stored_id = str(mystery["id"])
            if stored_id in self.resolved_mysteries:
                return "This mystery is already resolved: " + self.resolved_mysteries[stored_id]
            missing_mysteries = [
                required for required in mystery.get("required_mystery_ids", [])
                if mystery.get("type") == "murder" and str(required).casefold() not in {known.casefold() for known in self.resolved_mysteries}
            ]
            if missing_mysteries:
                return json.dumps(
                    {
                        "resolved": False,
                        "failure_reason": "The murder cannot be resolved until its existing prerequisite mystery or mysteries are resolved.",
                        "unresolved_prerequisite_mysteries": missing_mysteries,
                    },
                    ensure_ascii=False,
                )

            if not resolution_sufficient:
                return json.dumps(
                    {
                        "resolved": False,
                        "failure_reason": reasoning.strip() or "The player's argument does not yet meet the resolution standard.",
                        "resolution_requirement": str(mystery.get("resolution_requirement", "a relevant question, clue, or reasoned explanation")),
                        "answer_withheld": True,
                    },
                    ensure_ascii=False,
                )

            answer = str(mystery["answer"])
            self.staged_mysteries[stored_id] = answer
            return json.dumps({"resolved": True, "mystery_id": stored_id, "answer": answer}, ensure_ascii=False)
        return "No mystery with that ID exists."

    def commit_turn_knowledge(self) -> None:
        self.known_clues.update(self.staged_clues)
        self.resolved_mysteries.update(self.staged_mysteries)
        self.known_character_information.update(self.staged_character_information)
        self.known_facts.extend(fact for fact in self.staged_facts if fact not in self.known_facts)
        self.staged_clues.clear()
        self.staged_mysteries.clear()
        self.staged_character_information.clear()
        self.staged_facts.clear()

    def discard_staged_knowledge(self) -> None:
        self.staged_clues.clear()
        self.staged_mysteries.clear()
        self.staged_character_information.clear()
        self.staged_facts.clear()

    def compact_context(self) -> str:
        """A small program-owned memory summary, separate from model chat history."""
        case = self.case_file or {}
        overview = case.get("overview", {})
        role = case.get("player_role", {})
        return "\n".join(
            [
                "GAME MEMORY (trusted application state; use this to preserve continuity):",
                f"Case: {json.dumps(overview, ensure_ascii=False)}",
                f"Player role: {json.dumps(role, ensure_ascii=False)}",
                "Clues the player has discovered:",
                json.dumps(self.known_clues, ensure_ascii=False) if self.known_clues else "None yet.",
                "Mysteries the player has resolved:",
                json.dumps(self.resolved_mysteries, ensure_ascii=False) if self.resolved_mysteries else "None yet.",
                "Character secrets/information revealed to the player:",
                json.dumps(self.known_character_information, ensure_ascii=False) if self.known_character_information else "None yet.",
                "Other facts explicitly established to the player:",
                json.dumps(self.known_facts, ensure_ascii=False) if self.known_facts else "None recorded yet.",
                "Case additions made during play:",
                json.dumps(self.case_additions, ensure_ascii=False) if self.case_additions else "None.",
                "Recent conversation:",
                *(
                    f"Player: {player}\nNarrator: {narrator}"
                    for player, narrator in self.recent_turns[-MAX_RECENT_TURNS:]
                ),
            ]
        )


def validate_case_file(raw: str) -> dict[str, Any]:
    try:
        case = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Case file must be valid JSON: {exc}") from exc
    if not isinstance(case, dict):
        raise ValueError("Case file must be a JSON object.")

    required = {
        "overview",
        "player_role",
        "private_solution",
        "timeline",
        "characters",
        "clues",
        "mysteries",
        "opening",
        "consistency_check",
    }
    missing = sorted(required - case.keys())
    if missing:
        raise ValueError("Missing required case-file keys: " + ", ".join(missing))
    if not isinstance(case["opening"], str) or not case["opening"].strip():
        raise ValueError("The player-facing opening must be a non-empty string.")
    if not isinstance(case["overview"], dict):
        raise ValueError("'overview' must be an object.")
    for key in ("clues", "characters", "mysteries", "timeline"):
        if not isinstance(case[key], list):
            raise ValueError(f"'{key}' must be a JSON array.")

    def indexed(items: list[Any], label: str, key: str) -> dict[str, dict[str, Any]]:
        if any(not isinstance(item, dict) or not str(item.get(key, "")).strip() for item in items):
            raise ValueError(f"Every {label} must be an object with a non-empty '{key}'.")
        pairs = [(str(item[key]).strip(), item) for item in items]
        if len({name.casefold() for name, _ in pairs}) != len(pairs):
            raise ValueError(f"{label.capitalize()} IDs/names must be unique, ignoring letter case.")
        return {name: item for name, item in pairs}

    clues = indexed(case["clues"], "clue", "id")
    characters = indexed(case["characters"], "character", "name")
    mysteries = indexed(case["mysteries"], "mystery", "id")
    mysteries_by_fold = {key.casefold(): value for key, value in mysteries.items()}
    mystery_ids = {key.casefold() for key in mysteries}
    character_names = {key.casefold() for key in characters}

    if len(case["characters"]) < 3:
        raise ValueError("The case must have at least three substantial characters.")
    suspects = [c for c in case["characters"] if c.get("is_suspect") is True]
    if len(suspects) < 2:
        raise ValueError("The case must have at least two initially plausible suspects.")
    if len([c for c in case["characters"] if c.get("importance") == "major"]) < 3:
        raise ValueError("The case must have at least three major characters.")
    for character in suspects:
        if character.get("importance") != "major":
            raise ValueError(f"Suspect {character['name']} must be a major character.")
        for field_name in ("apparent_motive", "apparent_opportunity", "suspicious_circumstances"):
            if not character.get(field_name):
                raise ValueError(f"Suspect {character['name']} needs '{field_name}'.")
    for character in case["characters"]:
        if not isinstance(character.get("is_suspect"), bool):
            raise ValueError(f"Character {character['name']} needs a boolean is_suspect field.")
        if character.get("importance") not in {"major", "minor"}:
            raise ValueError(f"Character {character['name']} needs importance 'major' or 'minor'.")
        if character.get("importance") == "major" and not character.get("investigative_hook"):
            raise ValueError(f"Major character {character['name']} needs an investigative_hook.")
        if character.get("importance") == "major" and not str(character.get("theory_or_lead", "")).strip():
            raise ValueError(f"Major character {character['name']} needs a public theory_or_lead.")
        guarded = character.get("guarded_information", [])
        if not isinstance(guarded, list):
            raise ValueError(f"Character {character['name']} needs guarded_information as an array.")
        guarded_ids: set[str] = set()
        guarded_categories: set[str] = set()
        for item in guarded:
            if not isinstance(item, dict) or not all(
                isinstance(item.get(field), str) and item[field].strip()
                for field in ("id", "category", "content", "disclosure_requirement")
            ):
                raise ValueError(f"Character {character['name']} has invalid guarded_information; each entry needs id, category, content, and disclosure_requirement.")
            if item["id"].casefold() in guarded_ids:
                raise ValueError(f"Character {character['name']} repeats a guarded_information ID.")
            guarded_ids.add(item["id"].casefold())
            guarded_categories.add(item["category"].casefold())
            linked_mystery = str(item.get("linked_mystery_id", "")).casefold()
            if linked_mystery and linked_mystery not in mystery_ids:
                raise ValueError(f"Character {character['name']} has guarded information linked to an unknown mystery.")
        if character.get("is_suspect") is True and "motive" not in guarded_categories:
            raise ValueError(f"Suspect {character['name']} must mark their motive as guarded information.")
        if character.get("is_suspect") is True:
            motive = str(character.get("apparent_motive", "")).strip().casefold()
            guarded_motives = [
                str(item.get("content", "")).casefold()
                for item in guarded
                if str(item.get("category", "")).casefold() == "motive"
            ]
            if not motive or not any(motive in guarded_content for guarded_content in guarded_motives):
                raise ValueError(f"Suspect {character['name']}'s apparent_motive must be included in its guarded motive content.")

    auxiliary_mysteries = [m for m in case["mysteries"] if m.get("type") == "submystery"]
    if len(auxiliary_mysteries) < 3:
        raise ValueError("The case must contain at least three auxiliary submysteries.")
    if not any(m.get("red_herring") is True for m in auxiliary_mysteries):
        raise ValueError("At least one auxiliary mystery must be a genuine red herring.")
    murder = [m for m in case["mysteries"] if m.get("type") == "murder"]
    if len(murder) != 1 or murder[0].get("id") != "murder" or murder[0].get("scale") != 5:
        raise ValueError("There must be exactly one scale-5 murder mystery with ID 'murder'.")
    if not isinstance(case["private_solution"], dict) or not case["private_solution"].get("culprit"):
        raise ValueError("private_solution must be an object with a culprit character name.")
    culprit = str(case["private_solution"]["culprit"]).casefold()
    for field_name in ("motive", "method", "true_opportunity", "intended_outcome", "proof_summary"):
        if not case["private_solution"].get(field_name):
            raise ValueError(f"private_solution needs '{field_name}'.")
    if culprit not in character_names:
        raise ValueError("The private_solution culprit must match a character name.")
    if culprit not in {str(c["name"]).casefold() for c in suspects}:
        raise ValueError("The culprit must be among the initially plausible suspects.")

    def check_refs(owner: dict[str, Any], key: str, valid_ids: set[str], label: str) -> list[str]:
        refs = owner.get(key, [])
        if not isinstance(refs, list) or any(str(ref).casefold() not in valid_ids for ref in refs):
            raise ValueError(f"{label} has an unknown or invalid {key} reference.")
        folded_refs = [str(ref).casefold() for ref in refs]
        if len(set(folded_refs)) != len(folded_refs):
            raise ValueError(f"{label} repeats an ID in {key}.")
        return folded_refs

    for character in case["characters"]:
        for field_name in ("clues_knowledge", "mystery_knowledge"):
            if not isinstance(character.get(field_name), list):
                raise ValueError(f"Character {character['name']} needs {field_name} as an array.")
        if character.get("importance") == "major" and not (
            character["clues_knowledge"] or character["mystery_knowledge"]
        ):
            raise ValueError(f"Major character {character['name']} needs at least one clue or mystery knowledge lead.")
        for field_name, linked_fields in (
            ("clues_knowledge", ("clue_id", "mystery_id")),
            ("mystery_knowledge", ("mystery_id", "clue_id")),
        ):
            entries = character.get(field_name, [])
            if not isinstance(entries, list):
                raise ValueError(f"Character {character['name']} needs {field_name} as an array.")
            seen_links: set[tuple[str, str]] = set()
            for entry in entries:
                if not isinstance(entry, dict) or not all(
                    isinstance(entry.get(key), str) and entry[key].strip()
                    for key in (*linked_fields, "description")
                ):
                    raise ValueError(
                        f"Character {character['name']} has invalid {field_name}; each entry needs "
                        f"{', '.join((*linked_fields, 'description'))}."
                    )
                mystery_id = entry["mystery_id"].casefold()
                clue_id = entry["clue_id"].casefold()
                if mystery_id not in mystery_ids:
                    raise ValueError(f"Character {character['name']} has {field_name} linked to an unknown mystery.")
                if clue_id not in {str(clue['id']).casefold() for clue in case['clues']}:
                    raise ValueError(f"Character {character['name']} has {field_name} linked to an unknown clue.")
                linked_clue = next(clue for clue in case["clues"] if str(clue["id"]).casefold() == clue_id)
                if mystery_id not in {str(value).casefold() for value in linked_clue.get("mystery_ids", [])}:
                    raise ValueError(f"Character {character['name']} has {field_name} linked to a clue that does not inform that mystery.")
                link = (mystery_id, clue_id)
                if link in seen_links:
                    raise ValueError(f"Character {character['name']} repeats a mystery/clue link in {field_name}.")
                seen_links.add(link)

    for mystery in case["mysteries"]:
        if mystery.get("type") not in {"murder", "submystery"}:
            raise ValueError(f"Mystery {mystery['id']} needs type 'murder' or 'submystery'.")
        for field_name in ("title", "question", "answer"):
            if not isinstance(mystery.get(field_name), str) or not mystery[field_name].strip():
                raise ValueError(f"Mystery {mystery['id']} needs '{field_name}'.")
        if not isinstance(mystery.get("resolution_requirement"), str) or not mystery["resolution_requirement"].strip():
            raise ValueError(f"Mystery {mystery['id']} needs a resolution_requirement for the Narrator to assess.")
        scale = mystery.get("scale")
        if not isinstance(scale, int) or not 1 <= scale <= 5:
            raise ValueError(f"Mystery {mystery['id']} needs an integer scale from 1 to 5.")
        if not isinstance(mystery.get("red_herring"), bool):
            raise ValueError(f"Mystery {mystery['id']} needs a boolean red_herring field.")
        required_mysteries = check_refs(mystery, "required_mystery_ids", mystery_ids, mystery["id"])
        involved = check_refs(mystery, "involved_characters", character_names, mystery["id"])
        if not involved:
            raise ValueError(f"Mystery {mystery['id']} needs at least one involved character.")
        if mystery["id"].casefold() in required_mysteries:
            raise ValueError(f"Mystery {mystery['id']} cannot require itself.")
        if mystery.get("type") == "murder":
            auxiliary_gates = [ref for ref in required_mysteries if ref != "murder"]
            if not 1 <= len(auxiliary_gates) <= 2:
                raise ValueError("The murder must require only one or two necessary auxiliary mysteries.")
            if any(mysteries_by_fold[ref]["type"] != "submystery" for ref in auxiliary_gates):
                raise ValueError("The murder mystery's prerequisites must be auxiliary submysteries.")
            if len(auxiliary_gates) < 2 and mysteries_by_fold[auxiliary_gates[0]].get("scale", 0) < 4:
                raise ValueError("The murder must be guarded by two auxiliary mysteries or one scale-4 auxiliary mystery.")
        elif mystery.get("type") == "submystery":
            if required_mysteries:
                raise ValueError(f"Auxiliary mystery {mystery['id']} must not have hard required mystery IDs.")
        if mystery.get("type") == "submystery" and scale >= 5:
            raise ValueError("Only the central murder mystery may have scale 5.")

    for clue in case["clues"]:
        for field_name in ("name", "location", "observation", "objective_fact", "discovery_requirement"):
            if not isinstance(clue.get(field_name), str) or not clue[field_name].strip():
                raise ValueError(f"Clue {clue['id']} needs '{field_name}'.")
        level = clue.get("discovery_level")
        if not isinstance(level, int) or level not in (1, 2, 3):
            raise ValueError(f"Clue {clue['id']} needs discovery_level 1, 2, or 3.")
        check_refs(clue, "mystery_ids", mystery_ids, clue["id"])
        phrases = clue.get("required_action_phrases", [])
        if not isinstance(phrases, list) or any(not isinstance(p, str) for p in phrases):
            raise ValueError(f"Clue {clue['id']} needs a required_action_phrases array.")

    for character in suspects:
        if character["name"].casefold() != culprit:
            exoneration = str(character.get("exoneration_mystery_id", "")).casefold()
            if exoneration not in mystery_ids or exoneration == "murder":
                raise ValueError(f"Wrong suspect {character['name']} needs an auxiliary exoneration_mystery_id.")
            exoneration_mystery = mysteries_by_fold[exoneration]
            involved = {str(name).casefold() for name in exoneration_mystery.get("involved_characters", [])}
            if character["name"].casefold() not in involved:
                raise ValueError(f"Wrong suspect {character['name']}'s exoneration mystery must involve them.")

    return case


def create_designer_tools(state: GameState) -> list[Any]:
    @function_tool
    def save_case_file(case_json: str) -> str:
        """Validate and save the complete canonical case file in application memory."""
        if state.case_file is not None:
            return "A case file already exists. During play, do not replace or rewrite it. Use add_case_detail for a compatible addition."
        state.rejected_case_draft = case_json
        try:
            state.case_file = validate_case_file(case_json)
        except ValueError as exc:
            state.case_validation_errors.append(str(exc))
            return f"Case file was not saved: {exc} Please correct it and call this tool again."
        if DEBUG_MODE:
            debug_path = ROOT / "case_debug.json"
            try:
                debug_path.write_text(
                    json.dumps(state.case_file, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            except OSError as exc:
                state.case_file = None
                state.case_validation_errors.append(f"Could not write debug case file: {exc}")
                return f"Case file was not saved: debug case file could not be written to {debug_path}: {exc}"
        return "Case file saved and validated in memory. The private solution is not shown to the player."

    @function_tool
    def read_case_file(section: str = "all") -> str:
        """Read the canonical case file. Use a section name or 'all'."""
        if state.case_file is None:
            return "No case file has been saved yet."
        if section.casefold() == "all":
            result: Any = state.case_file
        elif section in state.case_file:
            result = state.case_file[section]
        else:
            return f"No case-file section named '{section}'."
        return json.dumps(result, ensure_ascii=False, indent=2)

    @function_tool
    def add_case_detail(detail: str, why_it_fits: str) -> str:
        """Append a small detail for an action that exposed a deliberate gap; never overwrite facts."""
        if state.case_file is None:
            return "Cannot add a detail before the case file exists."
        if not detail.strip() or not why_it_fits.strip():
            return "Provide both the detail and why it fits established facts."
        addition = {"detail": detail.strip(), "why_it_fits": why_it_fits.strip()}
        if addition not in state.case_additions:
            state.case_additions.append(addition)
        if detail.strip() not in state.staged_facts and detail.strip() not in state.known_facts:
            state.staged_facts.append(detail.strip())
        return "Addition recorded without changing the canonical case. It will count as player knowledge after this turn is delivered."

    @function_tool
    def add_character_knowledge(
        character_name: str,
        knowledge_type: str,
        clue_id: str,
        mystery_id: str,
        description: str,
        why_it_fits: str,
    ) -> str:
        """Append one Case Designer-approved, spoiler-safe character lead during a narrator hint request."""
        if not state.character_knowledge_request_active or state.case_file is None:
            return "Character knowledge can be added only during an active narrator hint request."
        if state.character_knowledge_added_this_request:
            return "Only one character knowledge entry may be added per hint request."
        if knowledge_type not in {"clue", "mystery"}:
            return "knowledge_type must be 'clue' or 'mystery'."
        if not description.strip() or not why_it_fits.strip():
            return "Provide both a non-empty description and why it fits the established case."
        expected = state.character_knowledge_request_target
        supplied = (character_name.casefold(), knowledge_type, clue_id.casefold(), mystery_id.casefold())
        if supplied != expected:
            return "Addition rejected: it must match the character and clue/mystery link requested by the Narrator."
        character = next(
            (item for item in state.case_file["characters"] if str(item["name"]).casefold() == character_name.casefold()),
            None,
        )
        clue = next((item for item in state.case_file["clues"] if str(item["id"]).casefold() == clue_id.casefold()), None)
        mystery = next((item for item in state.case_file["mysteries"] if str(item["id"]).casefold() == mystery_id.casefold()), None)
        if character is None or clue is None or mystery is None:
            return "Addition rejected: the character, clue, and mystery must already exist in the case."
        if mystery_id.casefold() not in {str(value).casefold() for value in clue.get("mystery_ids", [])}:
            return "Addition rejected: that clue does not inform the linked mystery."
        field_name = "clues_knowledge" if knowledge_type == "clue" else "mystery_knowledge"
        if any(
            str(entry.get("clue_id", "")).casefold() == clue_id.casefold()
            and str(entry.get("mystery_id", "")).casefold() == mystery_id.casefold()
            for entry in character.get(field_name, [])
        ):
            return "Addition rejected: this character already has knowledge linked to that clue and mystery."
        character.setdefault(field_name, []).append(
            {"clue_id": clue["id"], "mystery_id": mystery["id"], "description": description.strip()}
        )
        state.character_knowledge_added_this_request = True
        return json.dumps(
            {"character": character["name"], "knowledge_type": knowledge_type, "description": description.strip(), "why_it_fits": why_it_fits.strip()},
            ensure_ascii=False,
        )

    @function_tool
    def add_emergency_clue(
        id: str,
        name: str,
        location: str,
        observation: str,
        objective_fact: str,
        discovery_requirement: str,
        mystery_ids: list[str],
        discovery_level: int,
        what_it_does_not_prove: str,
    ) -> str:
        """Add one carefully checked safety-net clue to an active case when the Narrator requests it."""
        if not state.game_started or state.case_file is None or not state.emergency_clue_request_active:
            return "Cannot add an emergency clue before play begins."
        if state.emergency_clue_added_this_request:
            return "Only one emergency clue may be added per request."
        clue_id = id.strip()
        fields = (clue_id, name, location, observation, objective_fact, discovery_requirement, what_it_does_not_prove)
        if any(not value.strip() for value in fields):
            return "Every emergency clue field must be a non-empty string."
        if any(str(clue.get("id", "")).casefold() == clue_id.casefold() for clue in state.case_file["clues"]):
            return "That clue ID already exists. Choose a unique ID."
        if discovery_level not in (1, 2, 3):
            return "discovery_level must be 1, 2, or 3."
        valid_mysteries = {str(mystery["id"]).casefold() for mystery in state.case_file["mysteries"]}
        folded_mysteries = [str(mystery_id).casefold() for mystery_id in mystery_ids]
        if not folded_mysteries or len(set(folded_mysteries)) != len(folded_mysteries):
            return "List one or more unique mysteries informed by this clue."
        if any(mystery_id not in valid_mysteries for mystery_id in folded_mysteries):
            return "The clue references an unknown mystery ID."
        if not set(folded_mysteries).issubset(state.emergency_clue_target_mysteries):
            return "The clue may address only mystery gaps identified in the request."
        clue = {
            "id": clue_id,
            "name": name.strip(),
            "location": location.strip(),
            "observation": observation.strip(),
            "objective_fact": objective_fact.strip(),
            "discovery_requirement": discovery_requirement.strip(),
            "mystery_ids": mystery_ids,
            "discovery_level": discovery_level,
            "required_action_phrases": [],
            "what_it_does_not_prove": what_it_does_not_prove.strip(),
        }
        state.case_file["clues"].append(clue)
        state.emergency_clue_added_this_request = True
        return json.dumps(clue, ensure_ascii=False)

    return [save_case_file, read_case_file, add_case_detail, add_character_knowledge, add_emergency_clue]


def create_narrator_tools(state: GameState) -> list[Any]:
    @function_tool
    def look_up_case(section: str) -> str:
        """Look up case information for internal continuity, including the timeline; do not expose private timeline knowledge as dialogue."""
        if state.case_file is None:
            return "The case is not ready yet."
        if section in {"private_solution", "consistency_check"}:
            return "That section is restricted. Use public character information and discoverable clues; resolve mysteries through the gated tool."
        allowed = {"overview", "player_role", "timeline", "characters", "clues", "mysteries", "opening"}
        if section not in allowed:
            return "Unknown section. Available sections: " + ", ".join(sorted(allowed))

        if section == "overview":
            safe_fields = {"title", "premise", "tone", "setting", "special_circumstances"}
            result: Any = {key: value for key, value in state.case_file[section].items() if key in safe_fields}
        elif section == "characters":
            public_fields = {
                "name", "importance", "is_suspect", "description", "relationships",
                "apparent_opportunity", "public_alibi", "voice", "response_to_pressure",
                "theory_or_lead", "clues_knowledge", "mystery_knowledge",
            }
            result: Any = [
                {
                    **{key: value for key, value in character.items() if key in public_fields},
                    "guarded_information": [
                        {key: item[key] for key in ("id", "category", "disclosure_requirement", "linked_mystery_id") if key in item}
                        for item in character.get("guarded_information", [])
                    ],
                }
                for character in state.case_file["characters"]
            ]
        elif section == "clues":
            safe_fields = {
                "id", "discovery_level", "discovery_requirement",
            }
            result = [{key: value for key, value in clue.items() if key in safe_fields} for clue in state.case_file["clues"]]
        elif section == "mysteries":
            safe_fields = {
                "id", "type", "scale", "involved_characters", "resolution_requirement", "links_to_murder",
            }
            result = [{key: value for key, value in mystery.items() if key in safe_fields} for mystery in state.case_file["mysteries"]]
        else:
            result = state.case_file[section]
        return json.dumps(
            {"section": result, "additions": state.case_additions},
            ensure_ascii=False,
            indent=2,
        )

    @function_tool
    def inspect_character(character_name: str) -> str:
        """Read one character's public profile without revealing unresolved secrets."""
        if state.case_file is None:
            return "The case is not ready yet."
        for character in state.case_file["characters"]:
            if str(character.get("name", "")).casefold() == character_name.casefold():
                public_fields = {
                    "name", "importance", "is_suspect", "description", "relationships",
                    "apparent_opportunity", "public_alibi", "voice", "response_to_pressure",
                    "theory_or_lead", "clues_knowledge", "mystery_knowledge",
                }
                return json.dumps(
                    {
                        **{key: value for key, value in character.items() if key in public_fields},
                        "guarded_information": [
                            {key: item[key] for key in ("id", "category", "disclosure_requirement", "linked_mystery_id") if key in item}
                            for item in character.get("guarded_information", [])
                        ],
                        "already_known_guarded_information": {
                            key: value for key, value in state.known_character_information.items()
                            if key.casefold().startswith(character_name.casefold() + ":")
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                )
        return "No character with that name exists in the case file."

    @function_tool
    def discover_clue(clue_id: str, action_sufficient: bool, action_justification: str) -> str:
        """Stage a clue only when the Narrator judges the player's action and knowledge sufficient."""
        if state.case_file is None:
            return "The case is not ready yet."
        if clue_id in state.known_clues:
            return "The player already knows this clue: " + state.known_clues[clue_id]
        if len(action_justification.strip()) < 12:
            return "Action assessment too brief. No clue content was returned. Explain how the player's action and prior knowledge satisfy its discovery requirement."
        return state.stage_clue(clue_id, action_sufficient, action_justification)

    @function_tool
    def reveal_guarded_information(
        character_name: str, information_id: str, disclosure_sufficient: bool, justification: str
    ) -> str:
        """Reveal guarded information when earned; on failure, return the Narrator's reason without the secret."""
        if state.case_file is None:
            return "The case is not ready yet."
        character = next(
            (item for item in state.case_file["characters"] if str(item.get("name", "")).casefold() == character_name.casefold()),
            None,
        )
        if character is None:
            return "No character with that name exists in the case file."
        key = f"{character['name']}:{information_id}"
        if key in state.known_character_information:
            return "The player already knows this information: " + state.known_character_information[key]
        item = next(
            (entry for entry in character.get("guarded_information", []) if str(entry.get("id", "")).casefold() == information_id.casefold()),
            None,
        )
        if item is None:
            return "No guarded information with that ID exists for this character."
        if not disclosure_sufficient or len(justification.strip()) < 12:
            return json.dumps(
                {
                    "disclosed": False,
                    "failure_reason": justification.strip() or "The player's action and known information do not yet justify disclosure.",
                    "secret_withheld": True,
                },
                ensure_ascii=False,
            )
        state.staged_character_information[key] = str(item["content"])
        return json.dumps({"character": character["name"], "information": item["content"]}, ensure_ascii=False)

    @function_tool
    def get_player_knowledge() -> str:
        """Read the facts and clues that the application has recorded as known to the player."""
        return json.dumps(
            {
                "clues": state.known_clues,
                "resolved_mysteries": state.resolved_mysteries,
                "character_information": state.known_character_information,
                "facts": state.known_facts,
            },
            ensure_ascii=False,
            indent=2,
        )

    @function_tool
    def record_player_fact(fact: str) -> str:
        """Stage a concise, durable fact the narrator is about to tell the player."""
        cleaned = fact.strip()
        if not cleaned:
            return "No fact was recorded."
        if len(cleaned) > 500:
            return "Keep the recorded fact under 500 characters and focused on one thing the player can know."
        if cleaned not in state.known_facts and cleaned not in state.staged_facts:
            state.staged_facts.append(cleaned)
        return "Fact staged. It will be added to application memory after this narrator turn is delivered."

    @function_tool
    def resolve_mystery(mystery_id: str, resolution_sufficient: bool, reasoning: str) -> str:
        """Resolve a mystery after the Narrator's assessment; on failure, return its reason without the answer."""
        if state.case_file is None:
            return "The case is not ready yet."
        return state.stage_mystery_resolution(mystery_id, resolution_sufficient, reasoning)

    return [look_up_case, inspect_character, discover_clue, reveal_guarded_information, get_player_knowledge, record_player_fact, resolve_mystery]


def make_agents(state: GameState) -> tuple[Agent, Agent]:
    designer_instructions = read_agent_prompt("case_designer.md") + """

## Application tool protocol
The case is stored only through your tools, in application memory. You must call save_case_file with a valid JSON object before play begins. If validation rejects the draft, correct the reported problem and call save_case_file again; do not stop after the first rejection. Its required top-level keys are:
overview, player_role, private_solution, timeline, characters, clues, mysteries, opening, consistency_check.
Follow the detailed JSON schema and quality constraints in these instructions. Aim for at least four major characters and three initially plausible suspects, while meeting the minimum of three major characters and two suspects. Give every wrong suspect an exoneration mystery; give every major character an investigative hook and at least one clue or mystery knowledge lead; add a discovery_requirement to every clue and no clue prerequisites; mark every suspect's motive as guarded_information; make at least three auxiliary mysteries with resolution_requirements; and gate the scale-5 murder behind only one or two necessary auxiliary mysteries, with no required clue IDs. Once those mysteries are resolved, the Narrator should be able to resolve the murder after a compelling case. The app validates these requirements and will reject an incomplete case. The private_solution and timeline must contain enough detail to keep the mystery coherent.
Call read_case_file to check or reference the case. After play starts, never call save_case_file. Use add_case_detail only for a small compatible, non-decisive detail in a deliberate gap. Use add_emergency_clue only when the Narrator explicitly requests one through the stuck-player safeguard. Inspect the full canonical case, check every established fact and timeline constraint, and add exactly one fair, discoverable clue that addresses a specified unresolved mystery without changing the solution or retconning events. Do not turn it into a hard clue prerequisite or directly state a mystery answer. The application returns only the clue you added.
"""
    narrator_instructions = read_agent_prompt("narrator.md") + """

## Application tool protocol
Use look_up_case and inspect_character only as internal references; their clue/mystery indexes and guarded-information criteria are not player-facing. Character `theory_or_lead` is an ungated, public character perspective that may be voiced when relevant; use it to offer a fair nudge without turning it into proof. Compare the player's actual action and prior knowledge with a clue's discovery_requirement, then call discover_clue with your honest action_sufficient boolean and concise justification. Ordinary clues have no hard evidence prerequisites. For character secrets, use reveal_guarded_information and decide disclosure_sufficient from the player's actual action and known information. Never state an undiscovered clue's name, observation, or telltale feature. A character may mention an unresolved mystery's existence or broad question when it fits what they know, but prefer a nudge that lets the player discover the issue. Suspect motives are always guarded and must only be described after their guarded information is returned. A broad room sweep reveals only ordinary visible details.
The timeline is available as a private continuity reference. Use it to keep events, alibis, and time references consistent, but do not reveal an entry merely because it appears there. In narration and dialogue, state an exact time only when the player has learned it from an established fact, discovered clue, or a character who could know it. A character may know when they themselves moved or what they personally observed (for example, a character who left at 8:32 may report that movement if asked), but they do not know the time or details of the murder unless they witnessed it or committed it. Do not let a character state another person's private movements as fact without a plausible source. The culprit's knowledge of the murder remains concealed until the murder is resolved; do not use private timeline knowledge to hint at it.
Character `clues_knowledge` and `mystery_knowledge` are interview leads, not discovered evidence or mystery answers. Use them to make relevant characters feel informed and to offer a fair, appropriately vague lead when the player asks a related question or conversation naturally reaches it. Present each as that character's own observation, suspicion, or hearsay, with uncertainty matching its description. Do not volunteer unrelated leads, expose linked IDs, quote the internal description as an authoritative fact, describe an undiscovered clue as if found, or treat a lead as proof. Do not name an undiscovered clue or its telltale feature, but you may point toward a general place or line of inquiry (for example, suggest a closer look at the entrance or the ground nearby without mentioning footprints). The clue itself remains available only through `discover_clue` when its discovery requirement is met. A character may mention an unresolved mystery's existence or question, but must not explain or resolve it. Keep a culprit's incriminating knowledge concealed until the murder is resolved.
For any concealed mystery, call resolve_mystery with the mystery ID, your honest resolution_sufficient boolean, and a concise reasoning note based on their argument and the application memory of what they know. Auxiliary mysteries have no hard prerequisites. Python enforces only the murder's selected auxiliary-mystery prerequisites; no clues are required by ID. Never disclose a mystery answer if its tool call does not resolve it.
When resolve_mystery or reveal_guarded_information returns a failure_reason, use that reason in a brief in-world reaction: name the actual weakness in the player's argument or a relevant known alibi/defense, while keeping the guarded answer and internal clue IDs private. Do not invent evidence or merely say there is not enough proof; usually one or two sentences is enough.
Whenever you reveal another durable objective fact, call record_player_fact before finishing your response. Do not record atmosphere, speculation, or an unverified character claim as truth. Use consult_case_designer only for a genuinely open gap: pass the exact current player action, one narrow question, and all relevant mystery IDs. For a genuinely stuck player or an authored mystery with no fair discovery route, you may use request_emergency_clue sparingly. It is a safety net after ordinary hints and investigation have failed, not a shortcut: target only the unresolved mystery that is blocked, then gently point the player toward the new clue without revealing its observation until they discover it.
At the start of a turn, the application includes a compact memory of the premise, player role, discovered clues, resolved mysteries, guarded character information disclosed to the player, established facts, and the eight most recent exchanges. Treat that memory as authoritative. The application retains discoveries in memory after older dialogue is dropped from the prompt.
"""
    designer = Agent(
        name="Case Designer",
        model="gpt-6.0-sol",
        instructions=designer_instructions,
        tools=create_designer_tools(state),
    )

    @function_tool
    async def consult_case_designer(
        action_description: str,
        open_question: str,
        relevant_mystery_ids: list[str],
    ) -> str:
        """Ask for one compatible addition to an open story gap; unresolved mysteries cannot be consulted."""
        actual_action = " ".join(state.current_player_action.split())
        supplied_action = " ".join(action_description.split())
        if supplied_action.casefold() != actual_action.casefold():
            return "Consultation denied: action_description must match the player's current action verbatim."
        if not open_question.strip():
            return "Consultation denied: describe the specific open detail needed."

        mysteries = {str(m["id"]).casefold(): m for m in state.case_file["mysteries"]} if state.case_file else {}
        unresolved = []
        for mystery_id in relevant_mystery_ids:
            key = mystery_id.casefold()
            if key not in mysteries:
                return f"Consultation denied: unknown mystery ID {mystery_id}."
            if mystery_id.casefold() not in {item.casefold() for item in state.resolved_mysteries}:
                unresolved.append(mystery_id)
        if unresolved:
            return (
                "Consultation denied for unresolved mystery gate(s): " + ", ".join(unresolved)
                + ". Use clue and mystery tools; do not reveal or add facts that answer them."
            )

        before = len(state.case_additions)
        try:
            await Runner.run(
                designer,
                input=(
                    "The game is in progress. Check whether the following request concerns a genuinely unspecified, "
                    "non-decisive detail. If it can be answered without changing any established fact or resolving a "
                    "mystery, use add_case_detail to append the smallest compatible detail. Do not reveal an answer "
                    "to any mystery, the culprit, private solution, or an existing clue's hidden meaning. If the detail "
                    "is already fixed or is behind a mystery gate, do not append anything. Your final text may contain "
                    "private information; the application will discard it and return only appended open details.\n\n"
                    f"PLAYER ACTION (exact): {state.current_player_action}\n"
                    f"NARRATOR'S NARROW OPEN QUESTION: {open_question}\n"
                    f"ALREADY RESOLVED MYSTERIES: {json.dumps(list(state.resolved_mysteries), ensure_ascii=False)}"
                ),
                max_turns=6,
            )
        except Exception:
            return "The designer could not add a detail. Continue using established facts and clues, or leave the gap open."

        additions = state.case_additions[before:]
        if not additions:
            return "No new open detail was added. Use established case information or leave the unspecified detail open."
        return json.dumps([item["detail"] for item in additions], ensure_ascii=False)

    @function_tool
    async def request_character_knowledge_hint(
        character_name: str,
        knowledge_type: str,
        clue_id: str,
        mystery_id: str,
        proposed_description: str,
        hint_reason: str,
    ) -> str:
        """Ask the Case Designer to review and, if compatible, add one character lead during a player hint."""
        if not state.game_started or state.case_file is None:
            return "Character knowledge hints are available only during play."
        if not all(value.strip() for value in (character_name, knowledge_type, clue_id, mystery_id, proposed_description, hint_reason)):
            return "Request needs a character, knowledge type, clue ID, mystery ID, proposed lead, and hint reason."
        if knowledge_type not in {"clue", "mystery"}:
            return "knowledge_type must be 'clue' or 'mystery'."
        character = next((c for c in state.case_file["characters"] if str(c["name"]).casefold() == character_name.casefold()), None)
        clue = next((c for c in state.case_file["clues"] if str(c["id"]).casefold() == clue_id.casefold()), None)
        mystery = next((m for m in state.case_file["mysteries"] if str(m["id"]).casefold() == mystery_id.casefold()), None)
        if character is None or clue is None or mystery is None:
            return "Request denied: the character, clue, and mystery must exist in the case."
        if mystery_id.casefold() not in {str(value).casefold() for value in clue.get("mystery_ids", [])}:
            return "Request denied: that clue is not linked to the requested mystery."
        if mystery_id.casefold() in {key.casefold() for key in state.resolved_mysteries}:
            return "Request denied: use existing knowledge for an already resolved mystery."

        field_name = "clues_knowledge" if knowledge_type == "clue" else "mystery_knowledge"
        if any(
            str(entry.get("clue_id", "")).casefold() == clue_id.casefold()
            and str(entry.get("mystery_id", "")).casefold() == mystery_id.casefold()
            for entry in character.get(field_name, [])
        ):
            return "Request denied: this character already has a lead linked to that clue and mystery."

        state.character_knowledge_request_active = True
        state.character_knowledge_added_this_request = False
        state.character_knowledge_request_target = (
            character["name"].casefold(), knowledge_type, clue["id"].casefold(), mystery["id"].casefold()
        )
        try:
            await Runner.run(
                designer,
                input=(
                    "The Narrator is trying to give a fair, spoiler-free hint because the player appears stuck. "
                    "Read the full canonical case. Assess whether adding this one character knowledge lead is "
                    "plausible for the character, consistent with the timeline and established facts, and useful "
                    "without revealing a clue observation, proving a theory, resolving a mystery, exposing the culprit, "
                    "or creating a contradiction. You may use add_character_knowledge only if all checks pass; otherwise "
                    "add nothing. Do not alter or replace the case file. The application will return only an accepted lead.\n\n"
                    f"PLAYER ACTION (exact): {state.current_player_action}\n"
                    f"CHARACTER: {character['name']}\nKNOWLEDGE TYPE: {knowledge_type}\n"
                    f"LINKED CLUE ID: {clue['id']}\nLINKED MYSTERY ID: {mystery['id']}\n"
                    f"PROPOSED VAGUE LEAD: {proposed_description}\n"
                    f"WHY THIS HINT IS NEEDED: {hint_reason}\n"
                    f"ALREADY RESOLVED MYSTERIES: {json.dumps(list(state.resolved_mysteries), ensure_ascii=False)}"
                ),
                max_turns=5,
            )
        except Exception:
            return "The Case Designer could not review a character lead. Offer an ordinary spoiler-free nudge instead."
        finally:
            state.character_knowledge_request_active = False
            state.character_knowledge_request_target = None

        if not state.character_knowledge_added_this_request:
            return "The Case Designer did not approve a new lead. Offer an ordinary spoiler-free nudge instead."
        entry = character[field_name][-1]
        return json.dumps(
            {"character": character["name"], "knowledge_type": knowledge_type, "description": entry["description"]},
            ensure_ascii=False,
        )

    @function_tool
    async def request_emergency_clue(stuck_reason: str, relevant_mystery_ids: list[str]) -> str:
        """Rarely request one new clue when ordinary investigation cannot fairly advance an unresolved mystery."""
        if DEBUG_MODE:
            print("adding emergency clue")
        if not state.game_started or state.case_file is None:
            return "Emergency clue requests are available only during play."
        if not stuck_reason.strip():
            return "Describe the specific unfair or blocked mystery path."

        mysteries = {str(mystery["id"]).casefold(): mystery for mystery in state.case_file["mysteries"]}
        if not relevant_mystery_ids:
            return "Name the unresolved mystery that is blocked."
        target_ids: list[str] = []
        resolved_ids = {mystery_id.casefold() for mystery_id in state.resolved_mysteries}
        for mystery_id in relevant_mystery_ids:
            key = mystery_id.casefold()
            if key not in mysteries:
                return f"Emergency clue request denied: unknown mystery ID {mystery_id}."
            if key in resolved_ids:
                return f"Emergency clue request denied: mystery {mystery_id} is already resolved."
            target_ids.append(str(mysteries[key]["id"]))
        if len({value.casefold() for value in target_ids}) != len(target_ids):
            return "List each blocked mystery only once."

        murder = next((m for m in state.case_file["mysteries"] if m.get("type") == "murder"), None)
        if murder and any(
            str(prerequisite).casefold() not in resolved_ids
            for prerequisite in murder.get("required_mystery_ids", [])
        ) and any(value.casefold() == str(murder["id"]).casefold() for value in target_ids):
            return "Resolve the murder's existing auxiliary-mystery prerequisites first; do not add a clue to bypass them."

        previous_clue_ids = {str(clue["id"]).casefold() for clue in state.case_file["clues"]}
        state.emergency_clue_request_active = True
        state.emergency_clue_added_this_request = False
        state.emergency_clue_target_mysteries = {value.casefold() for value in target_ids}
        try:
            await Runner.run(
                designer,
                input=(
                    "The Narrator has invoked the rare emergency-clue safety valve. First inspect the canonical case "
                    "with read_case_file. Decide whether an existing clue or fair nudge already solves the issue. "
                    "If so, do not add anything. Otherwise use add_emergency_clue exactly once to add one clue that "
                    "fills the narrow missing inference/discovery path while preserving all established facts, the "
                    "timeline, and the existing solution. It must not bypass an unresolved murder prerequisite or "
                    "retcon events. But it should substantially help the player become unstuck. The application will "
                    "show the Narrator only a clue that you actually added.\n\n"
                    f"NARRATOR'S SPECIFIC STUCKNESS REPORT:\n{stuck_reason.strip()}\n\n"
                    f"TARGET UNRESOLVED MYSTERIES: {json.dumps(target_ids, ensure_ascii=False)}\n"
                    f"CURRENT PLAYER ACTION:\n{state.current_player_action}"
                ),
                max_turns=6,
            )
        except Exception:
            return "The Case Designer could not add a compatible clue. Continue with established evidence or leave the gap open."
        finally:
            state.emergency_clue_request_active = False
            state.emergency_clue_target_mysteries.clear()

        added = [
            clue for clue in state.case_file["clues"]
            if str(clue.get("id", "")).casefold() not in previous_clue_ids
        ]
        if not added:
            return "No emergency clue was added. Continue with fair hints and established evidence."
        state.emergency_clue_added_this_request = False
        return json.dumps(added[0], ensure_ascii=False)

    narrator = Agent(
        name="Narrator",
        model="gpt-6.0-luna",
        instructions=narrator_instructions,
        tools=[*create_narrator_tools(state), consult_case_designer, request_character_knowledge_hint, request_emergency_clue],
    )
    return designer, narrator


async def run_game() -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        print("OPENAI_API_KEY is not set.")
        print("In PowerShell, set it for this window with: $env:OPENAI_API_KEY = 'your-api-key'")
        raise SystemExit(1)

    print("Murder Mystery Chatbot")
    print("Describe the mystery you want (for example: a closed-room mystery in 19th-century London).")
    premise = input("Premise> ").strip()
    if not premise:
        print("Please provide a premise next time.")
        return

    state = GameState()
    designer, narrator = make_agents(state)

    print("\nThe Case Designer is preparing the mystery...\n")
    design_result = None
    design_errors: list[str] = []
    max_case_attempts = 3
    for attempt in range(1, max_case_attempts + 1):
        if attempt == 1:
            designer_input = (
                "Create a complete, internally consistent case file for this player's request. "
                "Follow your required application tool protocol and save it with save_case_file. "
                "After saving, your final message must be a short spoiler-free status only.\n\n"
                f"PLAYER REQUEST:\n{premise}"
            )
        elif state.rejected_case_draft:
            errors = state.case_validation_errors[-8:]
            previous_draft = state.rejected_case_draft
            designer_input = (
                "Repair the complete case-file draft below and save the corrected full case with save_case_file. "
                "Preserve its premise and good material while fixing every reported validation issue. "
                "Do not weaken validation requirements or omit required sections. If the save tool reports another "
                "problem, fix it and call the tool again. After a successful save, give only a short spoiler-free status.\n\n"
                f"PLAYER REQUEST:\n{premise}\n\n"
                f"LATEST VALIDATION FEEDBACK:\n{json.dumps(errors, ensure_ascii=False, indent=2)}\n\n"
                f"REJECTED CASE DRAFT TO REPAIR:\n{previous_draft}"
            )
        else:
            prior_note = design_errors[-1] if design_errors else (
                str(design_result.final_output) if design_result is not None else "No case file was saved."
            )
            designer_input = (
                "The previous case-creation attempt did not save a draft. Create a complete case file now and save it "
                "with save_case_file. Follow all validation requirements and retry the save if the tool reports an issue.\n\n"
                f"PLAYER REQUEST:\n{premise}\n\nPREVIOUS ATTEMPT STATUS:\n{prior_note}"
            )
        try:
            design_result = await Runner.run(designer, input=designer_input, max_turns=12)
        except Exception as exc:
            design_errors.append(str(exc))
            print(f"Case creation attempt {attempt}/{max_case_attempts} failed: {exc}")
        if state.case_file is not None:
            break
        if attempt < max_case_attempts:
            if state.case_validation_errors:
                print(f"The draft did not pass validation. Asking the Case Designer to repair it (attempt {attempt + 1}/{max_case_attempts})...")
            else:
                print(f"Retrying case creation (attempt {attempt + 1}/{max_case_attempts})...")
    if state.case_file is None:
        print("The Case Designer did not save a valid case file. Try again with a more specific premise.")
        if state.case_validation_errors:
            print("Last validation feedback:")
            for error in state.case_validation_errors[-4:]:
                print(f"- {error}")
        if design_errors:
            print(f"Last creation error: {design_errors[-1]}")
        if design_result is not None and design_result.final_output:
            print(f"Designer status: {design_result.final_output}")
        return

    state.game_started = True

    title = state.case_file.get("overview", {})
    if isinstance(title, dict):
        title = title.get("title", "Mystery")
    print(f"Case ready: {title}")
    print("Type /quit to end the game.\n")

    # Ask the Narrator to deliver the authored opening using the same console turn flow.
    player_action = "Begin the game. Deliver the case file's player-facing opening faithfully."
    first_turn = True
    while True:
        if not first_turn:
            try:
                player_action = input("\nYou> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nGame ended.")
                break
            if player_action.casefold() in {"/quit", "/exit", "quit", "exit"}:
                print("Game ended.")
                break
            if not player_action:
                continue

        first_turn = False
        state.discard_staged_knowledge()
        state.current_player_action = player_action
        turn_input = (
            state.compact_context()
            + "\n\nCURRENT PLAYER ACTION:\n"
            + player_action
            + "\n\nRespond in character and end by asking what the player wants to do next, unless the mystery has concluded."
        )
        try:
            result = await Runner.run(narrator, input=turn_input, max_turns=10)
        except Exception as exc:
            state.discard_staged_knowledge()
            print(f"\nThe narrator could not respond: {exc}")
            continue

        response = result.final_output if isinstance(result.final_output, str) else str(result.final_output)
        print(f"\n{response}")
        state.commit_turn_knowledge()
        state.recent_turns.append((player_action, response))
        state.recent_turns = state.recent_turns[-MAX_RECENT_TURNS:]

        if any(term in response.casefold() for term in ("case is closed", "mystery is solved", "the investigation is over")):
            print("\nThe story appears to have reached its conclusion. Type /quit to leave, or continue speaking to the Narrator.")


def main() -> None:
    try:
        asyncio.run(run_game())
    except KeyboardInterrupt:
        print("\nGoodbye.")


if __name__ == "__main__":
    main()
