"""Pure template parsing and stable rendering for automatic comments."""
from hashlib import sha256
import json
import random


def rule_revision(page):
    value = {key: page.get(key) for key in ("page_name", "delay_minutes")}
    for key in ("templates", "comment_sequence"):
        raw = page.get(key) or []
        value[key] = json.loads(raw) if isinstance(raw, str) else raw
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def select_template(templates, post_id):
    if not templates:
        raise ValueError("Page cần ít nhất một mẫu bình luận.")
    slot = int.from_bytes(sha256(post_id.encode()).digest()[:8], "big")
    return templates[slot % len(templates)]


def _parse(template):
    position = 0

    def sequence(depth=0):
        nonlocal position
        if depth > 30:
            raise ValueError("Spintax lồng quá nhiều cấp.")
        nodes, literal = [], []
        while position < len(template) and not (depth and template[position] in "|}"):
            char = template[position]
            position += 1
            if char == "}":
                raise ValueError("Dấu đóng ngoặc Spintax không hợp lệ.")
            if char != "{":
                literal.append(char)
                continue
            if literal:
                nodes.append("".join(literal))
                literal = []
            choices = [sequence(depth + 1)]
            while position < len(template) and template[position] == "|":
                position += 1
                choices.append(sequence(depth + 1))
            if position >= len(template) or template[position] != "}":
                raise ValueError("Cú pháp Spintax thiếu dấu đóng ngoặc.")
            position += 1
            if choices == [["page_name"]]:
                nodes.append(("variable",))
            elif len(choices) == 1 and all(isinstance(node, str) for node in choices[0]):
                raise ValueError("Biến nội dung chưa hỗ trợ; chỉ dùng {page_name} hoặc Spintax {A|B}.")
            else:
                if any(not choice or all(isinstance(node, str) and not node.strip() for node in choice) for choice in choices):
                    raise ValueError("Spintax có nhánh nội dung trống.")
                nodes.append(("choices", choices))
        if literal:
            nodes.append("".join(literal))
        return nodes

    tree = sequence()
    if position != len(template):
        raise ValueError("Dấu ngoặc hoặc dấu phân cách Spintax không hợp lệ.")
    return tree


def _max_length(nodes, page_name):
    return sum(len(node) if isinstance(node, str) else len(page_name) if node[0] == "variable"
               else max(_max_length(choice, page_name) for choice in node[1]) for node in nodes)


def validate_template(template, page_name):
    tree = _parse(template)
    if not template.strip() or _max_length(tree, page_name) > 8000:
        raise ValueError("Nội dung bình luận sau xử lý phải từ 1 đến 8000 ký tự.")
    return tree


def render(template, page_name, seed):
    tree = validate_template(template, page_name)
    rng = random.Random(sha256((seed + "\0" + template).encode()).digest())

    def visit(nodes):
        return "".join(node if isinstance(node, str) else page_name if node[0] == "variable"
                       else visit(rng.choice(node[1])) for node in nodes)

    value = visit(tree).strip()
    if not value or len(value) > 8000:
        raise ValueError("Nội dung bình luận sau xử lý phải từ 1 đến 8000 ký tự.")
    return value


def materialize(page, post_id, template=None, step=None, revision=None):
    revision = revision or rule_revision(page)
    if template is None:
        templates = page["templates"]
        template = select_template(json.loads(templates) if isinstance(templates, str) else templates, post_id)
    seed = f"{page.get('page_id', '')}:{post_id}:{step or 0}:{revision}"
    return {"template_source": template, "content_revision": revision,
            "content": render(template, page["page_name"], seed), "content_delay_minutes": page["delay_minutes"]}
