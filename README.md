# Murder Mystery Chatbot

A small console game built with the OpenAI Agents SDK. It creates a mystery with a GPT-5.6 Sol Case Designer, then runs play through a GPT-5.6 Luna Narrator.

## Setup

Python 3.10 or newer is required.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python main.py
```

Put `OPENAI_API_KEY=your-api-key` in a `.env` file beside `main.py`. `python-dotenv` loads it into the process environment. The `.env` file is ignored by Git; never commit or share it. The application uses the OpenAI API, so API usage may incur charges.

## How the first version works

- `agents/case_designer.md` and `agents/narrator.md` are loaded as the agents' instructions.
- The Case Designer creates a structured canonical case by calling its tools. The application validates and stores that case in memory.
- The Case Designer must create at least three plausible suspects, at least three auxiliary mysteries, and a murder solution gated behind clues and resolved mysteries. The app validates these requirements before play.
- The Narrator makes a semantic yes/no judgment for each clue discovery using the clue's `discovery_requirement`, the player's action, and what the player already knows. Ordinary clues have no hard prerequisite IDs.
- The Narrator also judges whether the player's argument and known information are enough to resolve auxiliary mysteries. Only the murder has hard required clue and auxiliary-mystery gates; Python checks those gates and a substantive current-turn argument. Resolutions are recorded only after the Narrator's response is delivered.
- Character motives and other information marked in `guarded_information` are omitted from public character profiles. The Narrator must use the gated disclosure tool and must not volunteer undiscovered secrets or motives.
- Major characters must have an investigative hook; wrong suspects need a clue-backed exoneration mystery.
- The application tracks discovered clues, resolved mysteries, disclosed character information, established facts, and a short recent conversation summary in memory. Nothing is saved as a game save file, so restarting the program starts a new case.
- The private case file is never printed directly to the console. The Narrator receives spoiler-safe sections and gated answers through tools.

Enter a premise when prompted, then type actions naturally. Enter `/quit` to end the game.
