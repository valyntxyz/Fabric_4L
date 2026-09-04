from __future__ import annotations

"""Tests for L4 provenance models and in-memory store."""


import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

# Add L4 source to path
_l4_src = str(Path(__file__).resolve().parents[1] / "src")
if _l4_src not in sys.path:
    sys.path.insert(0, _l4_src)

from layer4_agents.provenance.models import (
    PROVActivity,
    PROVAgent,
    PROVEntity,
    PROVGraph,
    PROVNamespace,
    PROVType,
    RDFStarTriple,
    create_prov_graph,
)
from layer4_agents.provenance.store import InMemoryTripleStore, create_triple_store

# ═══════════════════════════════════════════════════════════════════════════
# PROVNamespace
# ═══════════════════════════════════════════════════════════════════════════


class TestPROVNamespace:
    def test_core_namespaces(self):
        assert "prov" in PROVNamespace.PROV
        assert "rdf" in PROVNamespace.RDF
        assert "rdf-schema" in PROVNamespace.RDFS

    def test_get_prefixes(self):
        prefixes = PROVNamespace.get_prefixes()
        assert "prov" in prefixes
        assert "vf" in prefixes
        assert len(prefixes) == 5


# ═══════════════════════════════════════════════════════════════════════════
# PROVType
# ═══════════════════════════════════════════════════════════════════════════


class TestPROVType:
    def test_entity_types(self):
        assert PROVType.ENTITY.value == "prov:Entity"
        assert PROVType.COLLECTION.value == "prov:Collection"

    def test_vf_types(self):
        assert PROVType.LLM_MODEL.value == "vf:LLMModel"
        assert PROVType.AI_AGENT.value == "vf:AIAgent"


# ═══════════════════════════════════════════════════════════════════════════
# PROVEntity
# ═══════════════════════════════════════════════════════════════════════════


class TestPROVEntity:
    def test_auto_urn_generation(self):
        entity = PROVEntity(entity_id="my-entity")
        assert entity.entity_id == "urn:uuid:my-entity"

    def test_preserves_uri(self):
        entity = PROVEntity(entity_id="http://example.com/entity/1")
        assert entity.entity_id == "http://example.com/entity/1"

    def test_preserves_urn(self):
        entity = PROVEntity(entity_id="urn:uuid:abc-123")
        assert entity.entity_id == "urn:uuid:abc-123"

    def test_to_triples_basic(self):
        entity = PROVEntity(entity_id="urn:uuid:e1", label="Test Entity")
        triples = entity.to_triples()
        # Should have rdf:type and rdfs:label
        [t[0] for t in triples]
        predicates = [t[1] for t in triples]
        assert "rdf:type" in predicates
        assert "rdfs:label" in predicates

    def test_to_triples_with_attributes(self):
        entity = PROVEntity(
            entity_id="urn:uuid:e1",
            attributes={"confidence": 0.95, "source": "llm"},
        )
        triples = entity.to_triples()
        vf_preds = [t for t in triples if t[1].startswith("vf:")]
        assert len(vf_preds) == 2

    def test_to_triples_with_provenance(self):
        now = datetime.now(UTC)
        entity = PROVEntity(
            entity_id="urn:uuid:e1",
            generated_at=now,
            generated_by="urn:uuid:act-1",
        )
        triples = entity.to_triples()
        preds = {t[1] for t in triples}
        assert "prov:generatedAtTime" in preds
        assert "prov:wasGeneratedBy" in preds

    def test_to_dict(self):
        entity = PROVEntity(entity_id="urn:uuid:e1", label="Test")
        d = entity.to_dict()
        assert d["id_"] == "urn:uuid:e1"
        assert d["label"] == "Test"


# ═══════════════════════════════════════════════════════════════════════════
# PROVActivity
# ═══════════════════════════════════════════════════════════════════════════


class TestPROVActivity:
    def test_auto_urn(self):
        activity = PROVActivity(activity_id="act-1")
        assert activity.activity_id == "urn:uuid:act-1"

    def test_to_triples_with_relationships(self):
        now = datetime.now(UTC)
        activity = PROVActivity(
            activity_id="urn:uuid:act-1",
            started_at=now,
            ended_at=now,
            used_entities=["urn:uuid:e1"],
            generated_entities=["urn:uuid:e2"],
            was_associated_with=["urn:uuid:agent-1"],
        )
        triples = activity.to_triples()
        preds = {t[1] for t in triples}
        assert "prov:startedAtTime" in preds
        assert "prov:endedAtTime" in preds
        assert "prov:used" in preds
        assert "prov:wasAssociatedWith" in preds

    def test_generated_entities_produce_inverse_triple(self):
        activity = PROVActivity(
            activity_id="urn:uuid:act-1",
            generated_entities=["urn:uuid:e2"],
        )
        triples = activity.to_triples()
        # The generated entity triple has the entity as subject
        gen_triples = [t for t in triples if t[1] == "prov:wasGeneratedBy"]
        assert len(gen_triples) == 1
        assert gen_triples[0][0] == "urn:uuid:e2"

    def test_to_dict(self):
        activity = PROVActivity(activity_id="urn:uuid:act-1", label="Extraction")
        d = activity.to_dict()
        assert d["id_"] == "urn:uuid:act-1"
        assert d["label"] == "Extraction"


# ═══════════════════════════════════════════════════════════════════════════
# PROVAgent
# ═══════════════════════════════════════════════════════════════════════════


class TestPROVAgent:
    def test_auto_urn(self):
        agent = PROVAgent(agent_id="agent-1")
        assert agent.agent_id == "urn:uuid:agent-1"

    def test_delegation(self):
        agent = PROVAgent(
            agent_id="urn:uuid:agent-1",
            acted_on_behalf_of="urn:uuid:agent-0",
        )
        triples = agent.to_triples()
        delegation = [t for t in triples if t[1] == "prov:actedOnBehalfOf"]
        assert len(delegation) == 1

    def test_to_dict(self):
        agent = PROVAgent(
            agent_id="urn:uuid:agent-1",
            agent_type=PROVType.AI_AGENT,
            label="ROI Agent",
        )
        d = agent.to_dict()
        assert d["type_"] == "vf:AIAgent"
        assert d["label"] == "ROI Agent"


# ═══════════════════════════════════════════════════════════════════════════
# RDFStarTriple
# ═══════════════════════════════════════════════════════════════════════════


class TestRDFStarTriple:
    def test_to_rdf_star(self):
        triple = RDFStarTriple(
            subject=":calc123",
            predicate=":produces",
            object_=":roi_456",
            annotations={"vf:algorithmVersion": "2.1.0"},
        )
        turtle = triple.to_rdf_star()
        assert "<<" in turtle
        assert ":calc123" in turtle
        assert "2.1.0" in turtle

    def test_to_dict(self):
        triple = RDFStarTriple(
            subject="s",
            predicate="p",
            object_="o",
            annotations={"key": "val"},
        )
        d = triple.to_dict()
        assert d["subject"] == "s"
        assert d["annotations"] == {"key": "val"}


# ═══════════════════════════════════════════════════════════════════════════
# PROVGraph
# ═══════════════════════════════════════════════════════════════════════════


class TestPROVGraph:
    def test_default_graph_id(self):
        graph = PROVGraph()
        assert graph.graph_id.startswith("urn:uuid:")

    def test_custom_graph_id(self):
        graph = PROVGraph(graph_id="prov:my-workflow")
        assert graph.graph_id == "prov:my-workflow"

    def test_add_entity(self):
        graph = PROVGraph()
        entity = PROVEntity(entity_id="urn:uuid:e1")
        graph.add_entity(entity)
        assert "urn:uuid:e1" in graph.entities

    def test_add_activity(self):
        graph = PROVGraph()
        activity = PROVActivity(activity_id="urn:uuid:act-1")
        graph.add_activity(activity)
        assert "urn:uuid:act-1" in graph.activities

    def test_add_agent(self):
        graph = PROVGraph()
        agent = PROVAgent(agent_id="urn:uuid:agent-1")
        graph.add_agent(agent)
        assert "urn:uuid:agent-1" in graph.agents

    def test_to_triples_aggregates_all(self):
        graph = PROVGraph()
        graph.add_entity(PROVEntity(entity_id="urn:uuid:e1", label="E1"))
        graph.add_activity(PROVActivity(activity_id="urn:uuid:act-1", label="A1"))
        graph.add_agent(PROVAgent(agent_id="urn:uuid:agent-1", label="Ag1"))
        triples = graph.to_triples()
        subjects = {t[0] for t in triples}
        assert "urn:uuid:e1" in subjects
        assert "urn:uuid:act-1" in subjects
        assert "urn:uuid:agent-1" in subjects

    def test_to_turtle_contains_prefixes(self):
        graph = PROVGraph()
        graph.add_entity(PROVEntity(entity_id="urn:uuid:e1"))
        turtle = graph.to_turtle()
        assert "@prefix prov:" in turtle
        assert "@prefix vf:" in turtle

    def test_to_turtle_includes_rdf_star(self):
        graph = PROVGraph()
        graph.add_rdf_star(
            RDFStarTriple(
                subject=":s", predicate=":p", object_=":o",
                annotations={"vf:note": "test"},
            )
        )
        turtle = graph.to_turtle()
        assert "RDF* Annotations" in turtle
        assert "<<" in turtle

    def test_to_dict_structure(self):
        graph = PROVGraph(graph_id="prov:test")
        graph.add_entity(PROVEntity(entity_id="urn:uuid:e1"))
        d = graph.to_dict()
        assert d["id_"] == "prov:test"
        assert "prov" in d["context_"]
        assert len(d["entities"]) == 1


# ═══════════════════════════════════════════════════════════════════════════
# create_prov_graph factory
# ═══════════════════════════════════════════════════════════════════════════


class TestCreateProvGraph:
    def test_creates_graph_with_agent(self):
        now = datetime.now(UTC)
        graph = create_prov_graph(
            workflow_id="wf-1",
            agent_id="agent-1",
            inputs=[{"id": "doc-1", "name": "Document"}],
            outputs=[{"id": "result-1", "name": "ROI Report"}],
            started_at=now,
            ended_at=now,
        )
        assert len(graph.agents) == 1
        assert len(graph.activities) == 1
        assert len(graph.entities) == 2  # 1 input + 1 output

    def test_activity_references(self):
        now = datetime.now(UTC)
        graph = create_prov_graph(
            workflow_id="wf-1",
            agent_id="agent-1",
            inputs=[{"id": "in-1", "name": "Input"}],
            outputs=[{"id": "out-1", "name": "Output"}],
            started_at=now,
            ended_at=now,
        )
        activity = list(graph.activities.values())[0]
        assert len(activity.used_entities) == 1
        assert len(activity.generated_entities) == 1
        assert len(activity.was_associated_with) == 1


# ═══════════════════════════════════════════════════════════════════════════
# InMemoryTripleStore
# ═══════════════════════════════════════════════════════════════════════════


class TestInMemoryTripleStore:
    @pytest.fixture
    def store(self):
        return InMemoryTripleStore()

    @pytest.mark.asyncio
    async def test_store_and_query(self, store):
        graph = PROVGraph(graph_id="test-graph")
        graph.add_entity(PROVEntity(entity_id="urn:uuid:e1", label="Test"))
        graph_id = await store.store_graph(graph)
        assert graph_id == "test-graph"

        results = await store.query("SELECT * WHERE { ?s ?p ?o }")
        assert len(results) > 0

    @pytest.mark.asyncio
    async def test_store_rdf_star_annotations(self, store):
        graph = PROVGraph()
        graph.add_rdf_star(
            RDFStarTriple(
                subject="s", predicate="p", object_="o",
                annotations={"confidence": 0.95},
            )
        )
        await store.store_graph(graph)
        annotation = await store.get_rdf_star_annotation("s", "p", "o")
        assert annotation is not None
        assert annotation["confidence"] == 0.95

    @pytest.mark.asyncio
    async def test_lineage_empty(self, store):
        result = await store.get_lineage("nonexistent")
        assert result["entity_id"] == "nonexistent"
        assert result["upstream"] == []
        assert result["downstream"] == []

    @pytest.mark.asyncio
    async def test_upstream_lineage(self, store):
        graph = PROVGraph()
        # E1 --used--> Act1 --wasGeneratedBy--> E2
        entity1 = PROVEntity(entity_id="urn:uuid:e1", label="Input")
        entity2 = PROVEntity(
            entity_id="urn:uuid:e2",
            label="Output",
            generated_by="urn:uuid:act-1",
        )
        activity = PROVActivity(
            activity_id="urn:uuid:act-1",
            used_entities=["urn:uuid:e1"],
            generated_entities=["urn:uuid:e2"],
        )
        graph.add_entity(entity1)
        graph.add_entity(entity2)
        graph.add_activity(activity)
        await store.store_graph(graph)

        lineage = await store.get_lineage("urn:uuid:e2", direction="upstream")
        assert len(lineage["upstream"]) > 0
        upstream_entities = [u["entity"] for u in lineage["upstream"]]
        assert "urn:uuid:e1" in upstream_entities

    @pytest.mark.asyncio
    async def test_close(self, store):
        graph = PROVGraph()
        graph.add_entity(PROVEntity(entity_id="urn:uuid:e1"))
        await store.store_graph(graph)
        await store.close()
        # After close, store should be empty
        results = await store.query("SELECT *")
        assert len(results) == 0


class TestCreateTripleStore:
    @pytest.mark.asyncio
    async def test_memory_backend(self):
        store = await create_triple_store("memory")
        assert isinstance(store, InMemoryTripleStore)
        await store.close()

    @pytest.mark.asyncio
    async def test_unknown_backend_raises(self):
        with pytest.raises(ValueError, match="Unknown"):
            await create_triple_store("graphdb")
