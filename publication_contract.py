"""Cheap publication checks; no inference, source edits or public writes."""
from pathlib import Path
import re

RESPONSE_COMMIT_CONTRACT = '''For a READY response containing code changes, the controller commits
the independently approved patch, pushes it, then appends `Commit: <actual upstream commit URL>`
with the full new SHA to the reviewed reply. That future SHA cannot exist during generation or
review. Prepare the factual review-round summary now; do not invent a SHA, commit yourself, or
block solely because the controller has not committed yet. A REPLY without source changes gets
no new commit: cite only existing commits when relevant. All substantive checks still apply.
'''

def headings(text):
    # Comments and fenced examples are not visible sections of a PR description.
    text = re.sub(r'<!--.*?-->', '', text, flags=re.S)
    text = re.sub(r'^\s*(`{3,}|~{3,}).*?^\s*\1\s*$', '', text, flags=re.M | re.S)
    return re.findall(r'^##[ \t]+(.+?)[ \t]*#*[ \t]*$', text, re.M)

def required_sections(work, key):
    # OpenClaw's current default template requires each H2 section. Read the
    # checkout's template rather than freezing a historical list in the prompt.
    if key != 'openclaw':
        return []
    for name in ('.github/pull_request_template.md', '.github/PULL_REQUEST_TEMPLATE.md'):
        path = Path(work) / name
        if path.is_file():
            return headings(path.read_text())
    return []

def missing_sections(body, sections):
    visible = re.sub(r'<!--.*?-->', '', body, flags=re.S)
    visible = re.sub(r'^\s*(`{3,}|~{3,}).*?^\s*\1\s*$', '', visible, flags=re.M | re.S)
    matches = list(re.finditer(r'^##[ \t]+(.+?)[ \t]*#*[ \t]*$', visible, re.M))
    populated = set()
    for i, match in enumerate(matches):
        end = matches[i+1].start() if i+1 < len(matches) else len(visible)
        if visible[match.end():end].strip():
            populated.add(match.group(1).strip().casefold())
    return [s for s in sections if s.casefold() not in populated]

def prompt(work, key, kind):
    sections = required_sections(work, key)
    text = '\n' + RESPONSE_COMMIT_CONTRACT if kind == 'respond' else ''
    if sections:
        target = 'body' if kind == 'fix' else 'pr_body, if updating the PR description'
        text += ('\nPublication description contract: ' + target + ' must contain these visible '
                 'sections, each with truthful content supported by this exact patch and checks: '
                 + ', '.join('## '+s for s in sections) + '. Empty headings and HTML comments '
                 'do not satisfy this. Preserve risks, failed checks and AI disclosure.\n')
    return text
