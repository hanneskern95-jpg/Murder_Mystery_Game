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

## How this version works

- `agents/case_designer.md` and `agents/narrator.md` are loaded as the agents' instructions.
- The Case Designer creates and validates one structured, canonical case in memory.
- On every player turn, the Narrator receives the whole case file, the previous ten player/narrator rounds, discoveries, resolved mysteries, established facts, and every compatible addition made during play.
- The Narrator's instructions, rather than programmatic information lookups, protect secrets. It decides whether an action fairly discovers a clue, earns a character disclosure, or resolves a mystery, and records those outcomes for the next turn.
- The Case Designer can be consulted for a narrow canon-compatible detail, a vague character lead, or an emergency clue when an otherwise fair path is genuinely blocked.
- Major characters need an investigative hook; wrong suspects need a clue-backed exoneration mystery.
- Nothing is saved as a game save file, so restarting the program starts a new case.

Enter a premise when prompted, then type actions naturally. Enter `/quit` to end the game.
