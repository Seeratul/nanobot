"""Record the explicit review of the two paraphrased intro lines."""
import asyncio
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, '/tmp/nanobot-hindsight-dev')
import httpx
from nanobot.memory_sync.profiles import PROFILE_MODELS, ProfileExporter

STATE = Path('/home/nanobot/.nanobot/sessions/8679b657a17401f5f41c32a247d800ee')
WORKSPACE = Path('/home/nanobot/.nanobot/workspace')


async def main():
    report_path = STATE / 'hindsight-profile-import-report.json'
    report = json.loads(report_path.read_text())
    manifest = json.loads((STATE / 'hindsight-bootstrap.json').read_text())
    assert manifest['status'] == 'ready'
    checks = report['combined_consistency_check']
    assert not checks['unsupported_generated'] and not checks['contradictions']
    assert {(row['file'], row['line']) for row in checks['missing_essential_original']} == {
        ('SOUL.md', 3), ('USER.md', 3)}
    # The admin supplies only the fixed review unit's categorical journal output.
    # This process stays unprivileged and never invokes sudo itself.
    journal = sys.stdin.read()
    results = [json.loads(line) for line in journal.splitlines() if line.startswith('{"checks":')]
    assert len(results) == 1
    review = results[0]['checks']
    assert len(review) == 2 and {row['file'] for row in review} == {'SOUL.md', 'USER.md'}
    assert all(row['meaning_present_in_generated_profiles'] is True for row in review)
    async with httpx.AsyncClient(base_url='http://127.0.0.1:8888/v1/default/banks/personal',
                                 timeout=30, trust_env=False) as client:
        stats = (await client.get('/stats')).json()
        assert stats['total_documents'] == 2 and stats['pending_operations'] == 0
        for model_id, name in PROFILE_MODELS.items():
            original = (WORKSPACE / name).read_text()
            assert hashlib.sha256(original.encode()).hexdigest() == manifest['hashes'][model_id]
            model = (await client.get('/mental-models/' + model_id)).json()
            assert not model.get('last_refresh_failed_at')
            assert len(model['content']) == report['models'][model_id]['chars']
            report['models'][model_id]['reviewed_content_sha256'] = hashlib.sha256(model['content'].encode()).hexdigest()
        report['intro_line_review'] = review
        report['review_resolution'] = 'Both flagged intro statements are semantically present in the combined profiles.'
        report['consistency_passed'] = True
        report['unresolved_issues'] = []
        report['automatic_review_is_not_semantic_proof'] = True
        report['no_pending_operations_at_final_check'] = True
    ProfileExporter.write_atomic(report_path, json.dumps(report, indent=2))
    print(json.dumps(report), flush=True)


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception:
        print('{"status":"final_review_needs_attention"}', flush=True)
        raise SystemExit(1) from None
