"""Small offline Astra 19.2E starter profile; actual scanned services decide availability."""
import re

# Transponder frequencies in kHz. Scanning continues through the complete list.
TRANSPONDERS = ((11494000, 'H'), (11362000, 'H'), (11347000, 'V'), (11582000, 'H'),
                (11053000, 'H'), (10891000, 'H'), (12188000, 'H'), (12545000, 'H'))
CHANNELS = ('das erste', 'zdf', 'br fernsehen sud', 'wdr koln', 'ndr fs nds',
            'swr fernsehen bw', 'mdr s anhalt', 'rbb berlin', 'hr fernsehen',
            '3sat', 'arte', 'phoenix', 'tagesschau24', 'zdfinfo', 'zdfneo', 'one',
            'kika', 'rtl television', 'sat 1', 'prosieben', 'vox', 'kabel eins')


def name(value):
    value = value.casefold().replace('ü', 'u').replace('ö', 'o').replace('ä', 'a').replace('ß', 'ss')
    value = re.sub(r'\b(?:hd|sd)\b', '', value)
    value = re.sub(r'[^a-z0-9]+', ' ', value).strip()
    return {'zdf neo': 'zdfneo', 'swr bw': 'swr fernsehen bw', 'sat1': 'sat 1'}.get(value, value)


def lineup(candidates):
    groups = {}
    for channel in candidates:
        key = name(channel['name'])
        if key in CHANNELS:
            previous = groups.get(key)
            if previous is None or ('hd' in channel['name'].casefold() and 'hd' not in previous['name'].casefold()):
                groups[key] = channel
    return [groups[key]['service_uuid'] for key in CHANNELS if key in groups]


def mux_order(mux):
    try:
        pair = (int(mux.get('frequency', 0)), str(mux.get('polarisation', '')).upper())
        return TRANSPONDERS.index(pair)
    except (ValueError, TypeError):
        return len(TRANSPONDERS)
