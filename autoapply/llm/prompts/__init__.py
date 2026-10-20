from importlib import resources


def load(name: str, **values: str) -> str:
    text = resources.files(__package__).joinpath(f"{name}.md").read_text(encoding="utf-8")
    for k, v in values.items():
        text = text.replace("{{" + k + "}}", v)
    return text
