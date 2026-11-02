# 冻结证据离线复核：直接解析原始 HTML/JSON，不调用在线采集器或 AutoScraper。
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin

from .common import HOME, read_json, sha256, tasks, validate_evidence
from .scoring import score_records


@dataclass
class Node:
    tag: str
    attrs: dict = field(default_factory=dict)
    content: list = field(default_factory=list)

    def text(self):
        return "".join(item.text() if isinstance(item, Node) else item for item in self.content)

    # 仅提供本次证据所需的标签/类名查找，不引入通用 CSS 引擎或新依赖。
    def all(self, tag=None, cls=None):
        found = []
        for item in self.content:
            if isinstance(item, Node):
                if (tag is None or item.tag == tag) and (
                    cls is None or cls in item.attrs.get("class", "").split()
                ):
                    found.append(item)
                found.extend(item.all(tag, cls))
        return found

    def one(self, tag=None, cls=None):
        nodes = self.all(tag, cls)
        if len(nodes) != 1:
            raise ValueError(f"Expected one evidence element: {tag}/{cls}, got {len(nodes)}")
        return nodes[0]


class Document(HTMLParser):
    VOID = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }

    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.root = Node("document")
        self.stack = [self.root]
        self.feed(text)
        self.close()

    def handle_starttag(self, tag, attrs):
        node = Node(tag, dict(attrs))
        self.stack[-1].content.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].content.append(data)


# 每个字段均从已冻结源证据重建；累计滚动快照只读取最后一份，避免重复记录。
def records_from_evidence(task, truth):
    sources = truth["evidence"]
    if task["id"] == "local-network":
        return [
            row for source in sources for row in read_json(HOME / source["file"])["data"]["items"]
        ]
    documents = [
        (entry, Document((HOME / entry["file"]).read_text(encoding="utf-8")).root)
        for entry in sources
    ]
    records = []
    if task["id"].startswith("books"):
        for source, document in documents:
            for card in document.all("article", "product_pod"):
                link = card.one("h3").one("a")
                records.append(
                    {
                        "title": link.attrs["title"],
                        "price": card.one(cls="price_color").text(),
                        "url": urljoin(source["url"], link.attrs["href"]),
                    }
                )
        records = records[: task["count"]]
        if task["id"] == "books-detail":
            by_url = {source["url"]: document for source, document in documents}
            for row in records:
                document = by_url[row["url"]]
                # 描述必须是 product_description 标题之后紧邻的段落，不用全页简介代替。
                matches = []
                for parent in [document, *document.all()]:
                    siblings = [node for node in parent.content if isinstance(node, Node)]
                    for index, node in enumerate(siblings[:-1]):
                        if (
                            node.attrs.get("id") == "product_description"
                            and siblings[index + 1].tag == "p"
                        ):
                            matches.append(siblings[index + 1].text())
                if len(matches) != 1:
                    raise ValueError("Missing or ambiguous detail description evidence")
                row["description"] = matches[0]
    elif task["id"].startswith("quotes"):
        selected = documents[-1:] if task["id"] == "quotes-scroll" else documents
        for _, document in selected:
            for quote in document.all("div", "quote"):
                records.append(
                    {
                        "quote": quote.one(cls="text").text(),
                        "author": quote.one(cls="author").text(),
                        "tags": [node.text() for node in quote.one(cls="tags").all("a", "tag")],
                    }
                )
    else:
        source, document = documents[-1]
        for card in document.all("div", "test-sites-card"):
            link = card.one("h3", "card-title").one("a")
            years = [
                node.text()
                for node in card.all("p", "card-text")
                if node.text().strip().startswith("Year:")
            ]
            if len(years) != 1:
                raise ValueError("Ambiguous year evidence")
            records.append(
                {
                    "name": link.text(),
                    "description": card.one(cls="description").text(),
                    "year": years[0],
                    "status": card.one(cls="availability").one(cls="badge").text(),
                    "url": urljoin(source["url"], link.attrs["href"]),
                }
            )
    return records[: task["count"]]


def audit_task(task):
    truth = validate_evidence(task)
    result = score_records(task, truth["records"], records_from_evidence(task, truth))
    return {
        "task": task["id"],
        "truth_sha256": sha256(HOME / "truth" / f"{task['id']}.json"),
        "source_consistent": result["task_success"],
        "score": result,
        "human_review_required": truth.get("review", {}).get("status") != "human-reviewed",
    }


def audit_all():
    return [audit_task(task) for task in tasks().values()]
