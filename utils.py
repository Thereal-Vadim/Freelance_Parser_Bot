import re

def parse_indices(text):
    """
    Парсит строку с индексами и диапазонами вида "2, 5, 12-15".
    Возвращает отсортированный список уникальных целых чисел.
    """
    indices = set()
    # Заменяем запятые на пробелы и разбиваем по пробелам
    parts = text.replace(",", " ").split()
    for part in parts:
        part = part.strip()
        if not part:
            continue
        
        # Проверяем наличие диапазона типа "12-15"
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
