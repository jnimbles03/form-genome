import os

PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "prompts")

def load_prompt():
    with open(os.path.join(PROMPTS_DIR, "runtime.md"), "r") as f:
        return f.read()

def render(records, public_sector=False):
    prompt = load_prompt()
    # TODO: integrate with LLM
    return f"<!doctype html><html><body><h1>Report (runtime)</h1><p>{len(records)} records</p></body></html>"

def to_csv(records):
    # TODO: implement CSV export
    return "form_name,pages,complexity\n".encode()
