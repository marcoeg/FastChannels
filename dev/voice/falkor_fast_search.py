"""Hoist vecf32($search_vector) out of the per-row loop in FalkorDB searches.

graphiti-core 0.29.3 scores similarity with
    vec.cosineDistance(e.fact_embedding, vecf32($search_vector))
which re-converts the 1536-float query parameter to a vector ON EVERY ROW.
Measured on a 6,771-edge graph: >15,000 ms per search; with the conversion
hoisted into a leading `WITH vecf32($search_vector) AS qv`, the identical
scan takes ~12 ms — a >1000x difference. This module monkey-patches the two
similarity methods on FalkorSearchOperations with hoisted-cypher versions,
changing nothing else. Import it for its side effect. Candidate for an
upstream PR to getzep/graphiti.
"""

import logging

from graphiti_core.driver.driver import GraphProvider
from graphiti_core.driver.falkordb.operations.search_ops import (
    FalkorSearchOperations,
    _build_falkor_fulltext_query,
)
from graphiti_core.graph_queries import get_relationships_query
from graphiti_core.driver.query_executor import QueryExecutor
from graphiti_core.driver.record_parsers import (
    entity_edge_from_record,
    entity_node_from_record,
)
from graphiti_core.edges import EntityEdge
from graphiti_core.models.edges.edge_db_queries import get_entity_edge_return_query
from graphiti_core.models.nodes.node_db_queries import get_entity_node_return_query
from graphiti_core.nodes import EntityNode
from graphiti_core.search.search_filters import (
    SearchFilters,
    edge_search_filter_query_constructor,
    node_search_filter_query_constructor,
)

logger = logging.getLogger(__name__)


async def _fast_node_similarity_search(
    self,
    executor: QueryExecutor,
    search_vector: list[float],
    search_filter: SearchFilters,
    group_ids: list[str] | None = None,
    limit: int = 10,
    min_score: float = 0.6,
) -> list[EntityNode]:
    filter_queries, filter_params = node_search_filter_query_constructor(
        search_filter, GraphProvider.FALKORDB
    )

    if group_ids is not None:
        filter_queries.append('n.group_id IN $group_ids')
        filter_params['group_ids'] = group_ids

    filter_query = ''
    if filter_queries:
        filter_query = ' WHERE ' + (' AND '.join(filter_queries))

    cypher = (
        'WITH vecf32($search_vector) AS qv MATCH (n:Entity)'
        + filter_query
        + """
        WITH n, (2 - vec.cosineDistance(n.name_embedding, qv))/2 AS score
        WHERE score > $min_score
        RETURN
        """
        + get_entity_node_return_query(GraphProvider.FALKORDB)
        + """
        ORDER BY score DESC
        LIMIT $limit
        """
    )

    records, _, _ = await executor.execute_query(
        cypher,
        search_vector=search_vector,
        limit=limit,
        min_score=min_score,
        **filter_params,
    )

    return [entity_node_from_record(r) for r in records]


async def _fast_edge_similarity_search(
    self,
    executor: QueryExecutor,
    search_vector: list[float],
    source_node_uuid: str | None,
    target_node_uuid: str | None,
    search_filter: SearchFilters,
    group_ids: list[str] | None = None,
    limit: int = 10,
    min_score: float = 0.6,
) -> list[EntityEdge]:
    filter_queries, filter_params = edge_search_filter_query_constructor(
        search_filter, GraphProvider.FALKORDB
    )

    if group_ids is not None:
        filter_queries.append('e.group_id IN $group_ids')
        filter_params['group_ids'] = group_ids

        if source_node_uuid is not None:
            filter_params['source_uuid'] = source_node_uuid
            filter_queries.append('n.uuid = $source_uuid')

        if target_node_uuid is not None:
            filter_params['target_uuid'] = target_node_uuid
            filter_queries.append('m.uuid = $target_uuid')

    filter_query = ''
    if filter_queries:
        filter_query = ' WHERE ' + (' AND '.join(filter_queries))

    cypher = (
        # Unlabeled node pattern on purpose: with :Entity labels and no
        # selective node anchor, FalkorDB's planner joins two label scans
        # (~10 s at 17k edges) instead of one edge scan (~4 ms).
        'WITH vecf32($search_vector) AS qv MATCH (n)-[e:RELATES_TO]->(m)'
        + filter_query
        + """
        WITH DISTINCT e, n, m, (2 - vec.cosineDistance(e.fact_embedding, qv))/2 AS score
        WHERE score > $min_score
        RETURN
        """
        + get_entity_edge_return_query(GraphProvider.FALKORDB)
        + """
        ORDER BY score DESC
        LIMIT $limit
        """
    )

    records, _, _ = await executor.execute_query(
        cypher,
        search_vector=search_vector,
        limit=limit,
        min_score=min_score,
        **filter_params,
    )

    return [entity_edge_from_record(r) for r in records]


async def _fast_edge_fulltext_search(
    self,
    executor: QueryExecutor,
    query: str,
    search_filter: SearchFilters,
    group_ids: list[str] | None = None,
    limit: int = 10,
) -> list[EntityEdge]:
    """Fulltext leg without the O(matches x edges) re-match.

    The stock implementation joins every yielded relationship back with
    MATCH (n)-[e:RELATES_TO {uuid: rel.uuid}]->(m), a full edge scan per
    match — measured: query timeout (>15 s) on a 6,771-edge graph for a
    common term vs 3.4 ms using startNode()/endNode() on the yielded edge.
    """
    fuzzy_query = _build_falkor_fulltext_query(query, group_ids)
    if fuzzy_query == '':
        return []

    filter_queries, filter_params = edge_search_filter_query_constructor(
        search_filter, GraphProvider.FALKORDB
    )

    if group_ids is not None:
        filter_queries.append('e.group_id IN $group_ids')
        filter_params['group_ids'] = group_ids

    filter_query = ''
    if filter_queries:
        filter_query = ' WHERE ' + (' AND '.join(filter_queries))

    cypher = (
        get_relationships_query(
            'edge_name_and_fact', limit=limit, provider=GraphProvider.FALKORDB
        )
        + """
        YIELD relationship AS e, score
        WITH e, score, startNode(e) AS n, endNode(e) AS m
        """
        + filter_query
        + """
        WITH e, score, n, m
        RETURN
        """
        + get_entity_edge_return_query(GraphProvider.FALKORDB)
        + """
        ORDER BY score DESC
        LIMIT $limit
        """
    )

    records, _, _ = await executor.execute_query(
        cypher,
        query=fuzzy_query,
        limit=limit,
        **filter_params,
    )

    return [entity_edge_from_record(r) for r in records]


FalkorSearchOperations.node_similarity_search = _fast_node_similarity_search
FalkorSearchOperations.edge_similarity_search = _fast_edge_similarity_search
FalkorSearchOperations.edge_fulltext_search = _fast_edge_fulltext_search

# Class-level install: graphiti-core 0.29.3 never assigns
# driver.search_interface, so search_utils falls back to inline queries
# carrying the per-row vecf32 conversion — and FalkorDriver.clone() builds a
# FRESH driver for every group-scoped operation (falkordb_driver.py:342), so
# an instance-level assignment on the root driver would be bypassed by every
# per-group clone. A class attribute reaches root and clones alike;
# FalkorSearchOperations implements the whole SearchInterface dispatch
# surface with the same (driver, ...) argument shape.
from graphiti_core.driver.falkordb_driver import FalkorDriver  # noqa: E402

FalkorDriver.search_interface = FalkorSearchOperations()


def install(driver) -> None:
    """Kept for explicitness at the call site; the class attribute above
    already covers every FalkorDriver instance, including clones."""
    if getattr(driver, 'provider', None) == GraphProvider.FALKORDB:
        logger.info('falkor_fast_search: fast search interface active (class-level)')
