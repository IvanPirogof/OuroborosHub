#!/usr/bin/env python3
"""Snapshot skill Discussions and author-repository stars onto the signals branch."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

if __package__ in {None, ''}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.github_api import APIError, GitHub

CATEGORIES_QUERY = '''query($owner:String!,$repo:String!,$cursor:String) {
  repository(owner:$owner,name:$repo) { id hasDiscussionsEnabled
    discussionCategories(first:100,after:$cursor) {
      nodes { id name } pageInfo { hasNextPage endCursor }
    }
  }
}'''
DISCUSSIONS_QUERY = '''query($owner:String!,$repo:String!,$category:ID!,$cursor:String) {
  repository(owner:$owner,name:$repo) {
    discussions(first:100,categoryId:$category,after:$cursor,orderBy:{field:CREATED_AT,direction:ASC}) {
      nodes { title url upvoteCount comments { totalCount } }
      pageInfo { hasNextPage endCursor }
    }
  }
}'''
CREATE_DISCUSSION = '''mutation($repository:ID!,$category:ID!,$title:String!,$body:String!) {
  createDiscussion(input:{repositoryId:$repository,categoryId:$category,title:$title,body:$body}) {
    discussion { title url upvoteCount comments { totalCount } }
  }
}'''


def skills_category(api, variables):
    cursor = None
    while True:
        repo = api.graphql(CATEGORIES_QUERY, {**variables, 'cursor': cursor})['repository']
        if not repo or not repo['hasDiscussionsEnabled']:
            return None
        connection = repo['discussionCategories']
        for category in connection['nodes']:
            if category['name'] == 'Skills':
                return repo['id'], category['id']
        if not connection['pageInfo']['hasNextPage']:
            return None
        cursor = connection['pageInfo']['endCursor']


def discussion_rows(api, variables, category):
    cursor = None
    while True:
        connection = api.graphql(DISCUSSIONS_QUERY, {**variables, 'category': category, 'cursor': cursor})[
            'repository']['discussions']
        yield from connection['nodes']
        if not connection['pageInfo']['hasNextPage']:
            return
        cursor = connection['pageInfo']['endCursor']


def snapshot(api, repository, entries, *, generated_at=None):
    result = {'schema_version': 1,
              'generated_at': generated_at or datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
              'skills': {}, 'author_repos': {}}
    owner, repo = repository.split('/')
    variables = {'owner': owner, 'repo': repo}
    category = skills_category(api, variables)
    if category is None:
        return result
    repository_id, category_id = category
    discussions = {}
    for row in discussion_rows(api, variables, category_id):
        # Existing duplicates are not deleted. Reuse the oldest topic consistently.
        discussions.setdefault(row['title'], row)
    for name in sorted(entries):
        row = discussions.get(name)
        if row is None:
            row = api.graphql(CREATE_DISCUSSION, {
                'repository': repository_id, 'category': category_id, 'title': name,
                'body': f'Discuss [{name}](https://github.com/{repository}/blob/main/entries/{name}.json): '
                        'share examples, questions and feedback. Upvote this topic if the skill is useful to you.',
            })['createDiscussion']['discussion']
        votes, comments = row.get('upvoteCount'), (row.get('comments') or {}).get('totalCount')
        signal = {}
        if type(votes) is int and votes >= 0:
            signal['votes'] = votes
        if type(comments) is int and comments >= 0:
            signal['comments'] = comments
        if isinstance(row.get('url'), str) and row['url']:
            signal['discussion_url'] = row['url']
        result['skills'][name] = signal
    repos = {e['source']['repo'] for e in entries.values() if e['source']['kind'] == 'git'}
    for url in sorted(repos):
        parsed = urlsplit(url)
        parts = parsed.path.strip('/').removesuffix('.git').split('/')
        if parsed.scheme != 'https' or parsed.hostname != 'github.com' or len(parts) != 2:
            continue  # Unknown, never a synthetic zero.
        try:
            data = api.request('GET', f'/repos/{"/".join(parts)}')
        except APIError as exc:
            if exc.status in {403, 404, 451}:
                continue
            raise
        stars = data.get('stargazers_count')
        if type(stars) is int and stars >= 0:
            result['author_repos'][url] = {'stars': stars}
    return result


def publish(api, repository, document):
    """Create/update only refs/heads/signals, with an independent root on first run."""
    prefix = f'/repos/{repository}'
    try:
        parent = api.request('GET', f'{prefix}/git/ref/heads/signals')['object']['sha']
    except APIError as exc:
        if exc.status != 404:
            raise
        parent = None
    payload = json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + '\n'
    blob = api.request('POST', f'{prefix}/git/blobs', {'content': payload, 'encoding': 'utf-8'})
    tree = api.request('POST', f'{prefix}/git/trees', {'tree': [
        {'path': 'signals.json', 'mode': '100644', 'type': 'blob', 'sha': blob['sha']}]})
    commit = api.request('POST', f'{prefix}/git/commits', {
        'message': 'Refresh skill signals', 'tree': tree['sha'], 'parents': [parent] if parent else [],
        'author': {'name': 'Ouroboros', 'email': '311266734+ouroboros-agent@users.noreply.github.com'},
    })
    if parent:
        api.request('PATCH', f'{prefix}/git/refs/heads/signals', {'sha': commit['sha'], 'force': False})
    else:
        api.request('POST', f'{prefix}/git/refs', {'ref': 'refs/heads/signals', 'sha': commit['sha']})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('signals.json'))
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    try:
        entries = {p.stem: json.loads(p.read_text(encoding='utf-8')) for p in sorted((root / 'entries').glob('*.json'))}
        api = GitHub()
        repository = os.environ['GITHUB_REPOSITORY']
        document = snapshot(api, repository, entries)
        args.output.write_text(json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + '\n', encoding='utf-8')
        publish(api, repository, document)
        print(f"Published signals for {len(document['skills'])} skills to the signals branch")
    except (APIError, OSError, ValueError, RuntimeError, KeyError):
        print('Signals update failed; check API access and repository data', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
