import re
import random

def spin(text: str) -> str:
    """
    Parses and spins text formatted with Spintax syntax, e.g.:
    '{Chào|Xin chào|Hello} {bạn|anh chị}, shop {rất vui|hân hạnh} được hỗ trợ!'
    Nested spintax is also supported.
    """
    pattern = re.compile(r'\{([^{}]+)\}')
    while True:
        match = pattern.search(text)
        if not match:
            break
        choices = match.group(1).split('|')
        replacement = random.choice(choices)
        text = text[:match.start()] + replacement + text[match.end():]
    return text

if __name__ == "__main__":
    sample = "{Chào|Xin chào|Hello} {bạn|anh chị}, shop {đang có ưu đãi|tặng quà tri ân} nhé!"
    print("Example 1:", spin(sample))
    print("Example 2:", spin(sample))
    print("Example 3:", spin(sample))
