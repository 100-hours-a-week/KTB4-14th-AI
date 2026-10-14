"""Offline development DB snapshot, converted to the public backend DTO.

This module is never imported by the API or queue worker.
"""
from pathlib import Path

from dotenv import dotenv_values
import pymysql

from ai_service.matching.backend import BackendMatchingRequest
from ai_service.matching.dev import VERSION


def load_snapshot(env_path: Path):
    private = dotenv_values(env_path)
    with pymysql.connect(
        host=private['MATCHING_MYSQL_HOST'], port=int(private['MATCHING_MYSQL_PORT']),
        database=private['MATCHING_MYSQL_DATABASE'],
        user=private['MATCHING_SEED_USER'], password=private['MATCHING_SEED_PASSWORD'],
        cursorclass=pymysql.cursors.DictCursor, charset='utf8mb4',
    ) as conn, conn.cursor() as cursor:
        cursor.execute('START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY')
        cursor.execute('SELECT version,seed,fingerprint FROM matching_dataset_meta WHERE id=1')
        dataset = cursor.fetchone()
        if not dataset or dataset['version'] != VERSION:
            raise RuntimeError('Unknown evaluation dataset')
        cursor.execute('SELECT e.*,r.requester_id FROM matching_evaluation_cases e '
                       'JOIN matching_requests r ON r.id=e.matching_request_id ORDER BY e.matching_request_id')
        metadata = cursor.fetchall()
        cursor.execute('SELECT matching_request_id,candidate_id,relevance FROM matching_evaluation_labels')
        labels = {}
        for row in cursor.fetchall():
            labels.setdefault(row['matching_request_id'], {})[row['candidate_id']] = row['relevance']
        cursor.execute('SELECT id AS matching_request_id,requester_id AS user_id,'
                       'preferred_companion_gender,pace,budget_min,budget_max FROM matching_requests')
        requests = {row['matching_request_id']: {**row, 'theme': []} for row in cursor.fetchall()}
        cursor.execute('SELECT matching_request_id,theme FROM matching_requests_themes ORDER BY theme')
        for row in cursor.fetchall():
            requests[row['matching_request_id']]['theme'].append(row['theme'])
        cursor.execute('SELECT p.id,p.user_id,u.nickname,p.gender,p.pace FROM matching_profiles p '
                       "JOIN users u ON u.id=p.user_id WHERE p.is_active=1 AND u.status='ACTIVE' "
                       'AND u.deleted_at IS NULL ORDER BY p.user_id')
        profiles = {}
        for row in cursor.fetchall():
            profile_id = row.pop('id')
            profiles[profile_id] = {**row, 'theme': []}
        cursor.execute('SELECT matching_profile_id,theme FROM matching_profile_themes ORDER BY theme')
        for row in cursor.fetchall():
            if row['matching_profile_id'] in profiles:
                profiles[row['matching_profile_id']]['theme'].append(row['theme'])
    return dataset, metadata, labels, requests, list(profiles.values())


def backend_input(condition, profiles):
    return BackendMatchingRequest(request=condition,
        candidates=[p for p in profiles if p['user_id'] != condition['user_id']])
