"""Terms that distinguish soil and seabed substrate descriptions from chart notes."""

import re


# Include UK/US spellings and common adjective/plural forms. Generic words
# such as soil, seabed, sediment, soft and dense are not material evidence.
_SOIL_WORDS = """
    sand sands sandy clay clays clayey silt silts silty mud muds muddy
    gravel gravels gravelly shingle shingly pebble pebbles pebbly cobble
    cobbles cobbly boulder boulders bouldery rubble diamict diamicton
    diamictons diamictite peat peats peaty sapropel gyttja ooze oozes
    bedrock rock rocks rocky chalk chalky marl marls marly limestone
    limestones dolomite dolostone sandstone sandstones siltstone siltstones
    mudstone mudstones claystone shale shales slate conglomerate breccia
    granite basalt dolerite gabbro gneiss schist quartzite chert flint tuff
    coquina bioclasts bioclastic
"""
_SOIL_TERMS = re.compile(r'\b(?:' + '|'.join(_SOIL_WORDS.split()) + r'|'
                         r'(?:glacial|lodg[e]?ment|ablation|stiff|firm|soft) till|'
                         r'shell (?:hash|debris|fragments?|beds?)|(?:broken|whole) shells)\b')
_NON_SOIL_PHRASES = re.compile(
    r'\b(?:sand (?:waves?|banks?|ripples?|bags?)|mud volcano(?:es)?|clay pipes?|'
    r'gravel (?:berms?|extraction)|rubble mounds?|rock (?:berms?|dumps?|dumping|placement|armou?r|protection|bags?)|'
    r'dumped rock)\b'
)
_CHART_NOTE = re.compile(
    r'^(?:(?:kp|ch) *[0-9]|(?:chainage|survey limits?|route cent(?:re|er)line|'
    r'(?:cable|pipeline|route) crossing|(?:existing|proposed|out of service) (?:cable|pipeline))\b)'
)


def soil_terms(text):
    """Return distinct positive terms. Ignore common asset and terrain labels."""
    terms = set()
    for line in text.splitlines():
        words = re.sub(r'[\W_]+', ' ', line.casefold()).strip()
        if words == 'till':
            terms.add(words)
        elif not _CHART_NOTE.match(words):
            material_text = _NON_SOIL_PHRASES.sub(' ', words)
            terms.update(_SOIL_TERMS.findall(material_text))
    return sorted(terms)
