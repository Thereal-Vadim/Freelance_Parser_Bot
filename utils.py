import re

def parse_indices(text):
    """
    Parses a string containing indices and ranges like "2, 5, 12-15".
    Returns a sorted list of unique integers.
    """
    indices = set()
    # Replace commas with spaces and split by whitespace
    parts = text.replace(",", " ").split()
    for part in parts:
        part = part.strip()
        if not part:
            continue
        
        # Check for a range format like "12-15"
        range_match = re.match(r"^(\d+)-(\d+)$", part)
        if range_match:
            start = int(range_match.group(1))
            end = int(range_match.group(2))
            if start <= end:
                for i in range(start, end + 1):
                    indices.add(i)
        elif part.isdigit():
            indices.add(int(part))
            
    return sorted(list(indices))
