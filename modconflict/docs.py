"""Extract documentation claims; all documentation is inferred evidence."""
import re

LINK = re.compile(r'github\.com/[\w.-]+/([\w.-]+)', re.I)
PLUGIN = re.compile(r'\b[\w-]+\.(?:esp|esm|esl)\b', re.I)
QUOTED_PLUGIN = re.compile(r'[`\"]([^`\"\n]+\.(?:esp|esm|esl))[`\"]', re.I)


def plugin_names(text):
    names = QUOTED_PLUGIN.findall(text)
    return names + PLUGIN.findall(QUOTED_PLUGIN.sub(' ', text))


def extract(text):
    result = {'label': 'inferred', 'esl_claim': bool(re.search(r'\bESL(?:[- ]flagged)?\b', text, re.I)),
              'only_master_claim': bool(re.search(r'Skyrim\.esm\s+is\s+the\s+only\s+master|only\s+master\s*(?:is|:)\s*Skyrim\.esm', text, re.I)),
              'no_vanilla_edits_claim': bool(re.search(r'no vanilla records edited|edits no vanilla records|zero overrides|\bno edits\b', text, re.I)),
              'places_nothing_claim': bool(re.search(r'places nothing in the world|adds no cells', text, re.I)),
              'compatible_with': [], 'incompatible_with': [], 'load_after': [],
              'requires_text': [], 'mentions': [], 'series_wide': False, 'series_links': []}
    result['mentions'] = sorted(set([x.rstrip('.') for x in LINK.findall(text)] + plugin_names(text)), key=str.casefold)
    # Do not split file names or URLs at periods.
    sentences = re.split(r'(?<=[.!?])\s+|[\r\n]+', text)
    for sentence in sentences:
        repos = [x.rstrip('.') for x in LINK.findall(sentence)]
        incompatible = re.search(r'incompatible|conflicts with|do not use with', sentence, re.I)
        compatible = re.search(r'\bcompatible\b|works with', sentence, re.I)
        if incompatible:
            result['incompatible_with'].extend(repos + plugin_names(sentence))
        elif compatible:
            result['compatible_with'].extend(repos)
        if re.search(r'the rest of the series|all of it', sentence, re.I):
            result['series_wide'] = True
            result['series_links'].extend(repos)
        if re.search(r'load\b.*\bafter\b', sentence, re.I):
            result['load_after'].extend(plugin_names(re.split(r'\bafter\b', sentence, flags=re.I)[-1]))
        if re.search(r'\brequires?\b', sentence, re.I):
            # Store referenced names, rather than arbitrary documentation prose.
            result['requires_text'].extend(repos + plugin_names(sentence))
    for key, value in result.items():
        if isinstance(value, list):
            result[key] = sorted(set(value), key=str.casefold)
    return result
