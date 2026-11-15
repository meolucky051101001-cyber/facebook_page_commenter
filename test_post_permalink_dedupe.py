from contextlib import closing

import pytest

import database as db
from post_identity import facebook_post_key


PAGE = "1134157636440779"
ALIAS = "61564553242967"
OTHER_PAGE = "1134157636440780"
GRAPH_POST = f"{PAGE}_122118140852485108"
REEL = "1756655442063696"
REEL_URL = f"https://www.facebook.com/reel/{REEL}/"
WHEN = "2026-11-15T10:00:00+00:00"


@pytest.fixture
def pages():
    account = db.upsert_account("Test account")
    for page_id in (PAGE, OTHER_PAGE):
        db.upsert_page(page_id, "Test Page", "mock-token", account)
    with closing(db.get_connection()) as conn, conn:
        conn.execute("UPDATE pages SET fb_profile_id = ? WHERE page_id = ?", (ALIAS, PAGE))


def schedule(post_id=GRAPH_POST, url=REEL_URL):
    return {
        "id": post_id,
        "url": url,
        "created_time": WHEN,
        "steps": [
            {
                "number": number,
                "gap": 0 if number == 1 else 5,
                "content": f"Saved comment {number}",
                "scheduled_at": f"2026-11-15T10:{delay}:00+00:00",
                "template_source": "SHEET",
                "content_revision": "revision-1",
                "content_delay_minutes": delay,
            }
            for number, delay in enumerate((30, 35, 40), start=1)
        ],
    }


def saved_rows(conn):
    return [tuple(row) for row in conn.execute("SELECT * FROM tasks ORDER BY id")]


@pytest.mark.parametrize("url, expected", [
    (REEL_URL, ("reel", REEL)),
    (f"https://m.facebook.com/reel/{REEL}?mibextid=abc&fbclid=tracking", ("reel", REEL)),
    (f"https://facebook.com:443/reel/{REEL}/?__cft__[0]=tracking&__tn__=R", ("reel", REEL)),
    (f"https://www.facebook.com/videos/{REEL}/", ("video", REEL)),
    (f"https://www.facebook.com/{ALIAS}/videos/{REEL}/", ("video", REEL)),
    (f"https://www.facebook.com/{PAGE}/posts/{REEL}/", ("post", REEL)),
    (f"https://www.facebook.com/{ALIAS}/posts/{REEL}", ("post", REEL)),
    (f"https://www.facebook.com/permalink.php?story_fbid={REEL}&id={ALIAS}&ref=share", ("post", REEL)),
])
def test_supported_permalink_normalization(url, expected):
    assert facebook_post_key(url, allowed_owner_ids=(PAGE, ALIAS)) == expected


@pytest.mark.parametrize("url", [
    None,
    "",
    f"http://www.facebook.com/reel/{REEL}",
    f"https://www.facebook.com.evil.example/reel/{REEL}",
    f"https://evil.example/reel/{REEL}",
    f"https://user@www.facebook.com/reel/{REEL}",
    f"https://www.facebook.com:8443/reel/{REEL}",
    f"https://www.facebook.com:/reel/{REEL}",
    f" https://www.facebook.com/reel/{REEL}",
    f"https://www.facebook.com/reel/{REEL}\n",
    f"https://www.facebook.com\\reel\\{REEL}",
    f"https://www.facebook.com/reel/{REEL}/extra",
    f"https://www.facebook.com/reel/{REEL}#comment",
    f"https://www.facebook.com/reel/{REEL}?comment_id=",
    f"https://www.facebook.com/reel/{REEL}?reply_comment_id=123",
    f"https://www.facebook.com/reel/{REEL}?%63omment_id=123",
    f"https://www.facebook.com/reel/{REEL}?v={REEL}",
    f"https://www.facebook.com/reel/{REEL}?id={PAGE}",
    f"https://www.facebook.com/reel/{REEL}?story_fbid={REEL}",
    f"https://www.facebook.com/reel/{REEL}?fbid={REEL}",
    f"https://www.facebook.com/reel/{REEL}?sk=videos",
    f"https://www.facebook.com/{OTHER_PAGE}/posts/{REEL}",
    f"https://www.facebook.com/{OTHER_PAGE}/videos/{REEL}",
    f"https://www.facebook.com/page-name/posts/{REEL}",
    f"https://www.facebook.com/share/r/{REEL}/",
    f"https://www.facebook.com/{PAGE}/posts/pfbidExample",
    f"https://www.facebook.com/permalink.php?story_fbid={REEL}&id={OTHER_PAGE}",
    f"https://www.facebook.com/permalink.php?story_fbid={REEL}",
    f"https://www.facebook.com/permalink.php?story_fbid={REEL}&id={PAGE}&id={PAGE}",
    f"https://www.facebook.com/permalink.php?story_fbid={REEL}&id={PAGE}&id={OTHER_PAGE}",
    f"https://www.facebook.com/permalink.php?story_fbid={REEL}&story_fbid={REEL}&id={PAGE}",
    f"https://www.facebook.com/permalink.php?story_fbid={REEL}&id={PAGE}&fbid={REEL}",
    f"https://www.facebook.com/permalink.php?story_fbid=abc&id={PAGE}",
    f"https://www.facebook.com/permalink.php?story_fbid={REEL}&id=",
    f"https://www.facebook.com/permalink.php?STORY_FBID={REEL}&ID={PAGE}",
    f"https://www.facebook.com/permalink.php?story_fbid={REEL}&Id={PAGE}",
    f"https://www.facebook.com/permalink.php?story_fbid={REEL}&id={PAGE}&ID={OTHER_PAGE}",
])
def test_unsupported_or_ambiguous_permalink_has_no_identity(url):
    assert facebook_post_key(url, allowed_owner_ids=(PAGE, ALIAS)) is None


def test_graph_and_browser_reel_ids_share_one_unchanged_sequence(pages):
    with closing(db.get_connection()) as conn, conn:
        assert db.insert_auto_post_tasks(conn, PAGE, schedule())
        before = saved_rows(conn)
        assert len(before) == 3
        conn.commit()
        duplicate = schedule(f"{PAGE}_{REEL}", f"https://m.facebook.com/reel/{REEL}?mibextid=abc")
        duplicate["steps"][0]["content"] = "New content must not replace the saved chain"
        assert not db.insert_auto_post_tasks(conn, PAGE, duplicate)
        assert saved_rows(conn) == before


def test_duplicate_alias_is_blocked_within_same_transaction(pages):
    with closing(db.get_connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        assert db.insert_auto_post_tasks(conn, PAGE, schedule())
        assert not db.insert_auto_post_tasks(conn, PAGE, schedule(f"{PAGE}_{REEL}"))
        assert len(saved_rows(conn)) == 3


def test_different_reel_remains_schedulable(pages):
    with closing(db.get_connection()) as conn, conn:
        assert db.insert_auto_post_tasks(conn, PAGE, schedule())
        assert db.insert_auto_post_tasks(conn, PAGE, schedule(f"{PAGE}_999", "https://www.facebook.com/reel/999/"))
        assert len(saved_rows(conn)) == 6


def test_same_numeric_id_in_different_namespaces_is_distinct(pages):
    with closing(db.get_connection()) as conn, conn:
        urls = (REEL_URL, f"https://www.facebook.com/videos/{REEL}",
                f"https://www.facebook.com/{PAGE}/posts/{REEL}")
        for index, url in enumerate(urls):
            assert db.insert_auto_post_tasks(conn, PAGE, schedule(f"{PAGE}_{index}", url))
        assert len(saved_rows(conn)) == 9


@pytest.mark.parametrize("url", [
    f"https://www.facebook.com/{OTHER_PAGE}/posts/{REEL}",
    f"https://www.facebook.com/{OTHER_PAGE}/videos/{REEL}",
    f"https://evil.example/reel/{REEL}",
    f"https://www.facebook.com/page-name/posts/{REEL}",
    f"https://www.facebook.com/reel/{REEL}?comment_id=123",
    f"https://www.facebook.com/reel/{REEL}?v=999",
])
def test_unsafe_or_foreign_permalink_cannot_merge_distinct_post_ids(pages, url):
    with closing(db.get_connection()) as conn, conn:
        assert db.insert_auto_post_tasks(conn, PAGE, schedule(url=url))
        assert db.insert_auto_post_tasks(conn, PAGE, schedule(f"{PAGE}_{REEL}", url))
        assert len(saved_rows(conn)) == 6


def test_permalink_dedupe_stays_within_database_page(pages):
    with closing(db.get_connection()) as conn, conn:
        assert db.insert_auto_post_tasks(conn, OTHER_PAGE, schedule(f"{OTHER_PAGE}_123"))
        assert db.insert_auto_post_tasks(conn, PAGE, schedule())
        assert len(saved_rows(conn)) == 6


@pytest.mark.parametrize("dedupe_key, suppressed", [("manual:batch:123", False), ("publication:123", True)])
def test_publication_history_suppresses_auto_but_manual_batch_does_not(pages, dedupe_key, suppressed):
    assert db.create_task(PAGE, GRAPH_POST, "Existing comment", WHEN, source="MANUAL",
                          dedupe_key=dedupe_key, post_url=REEL_URL)
    with closing(db.get_connection()) as conn, conn:
        assert db.insert_auto_post_tasks(conn, PAGE, schedule(f"{PAGE}_{REEL}")) is not suppressed
        assert len(saved_rows(conn)) == (1 if suppressed else 4)


@pytest.mark.parametrize("url", [None, "https://unsupported.example/post/123"])
def test_exact_post_id_dedupe_does_not_require_supported_permalink(pages, url):
    with closing(db.get_connection()) as conn, conn:
        assert db.insert_auto_post_tasks(conn, PAGE, schedule(url=url))
        before = saved_rows(conn)
        assert not db.insert_auto_post_tasks(conn, PAGE, schedule())
        assert saved_rows(conn) == before


@pytest.mark.parametrize("alias_url", [
    f"https://m.facebook.com/{ALIAS}/posts/{REEL}",
    f"https://www.facebook.com/permalink.php?story_fbid={REEL}&id={ALIAS}",
])
def test_known_canonical_and_browser_owner_aliases_normalize_together(pages, alias_url):
    with closing(db.get_connection()) as conn, conn:
        assert db.insert_auto_post_tasks(conn, PAGE, schedule(url=f"https://www.facebook.com/{PAGE}/posts/{REEL}"))
        assert not db.insert_auto_post_tasks(conn, PAGE, schedule(f"{PAGE}_{REEL}", alias_url))
        assert len(saved_rows(conn)) == 3
