// Replace GRAPH_RUN_ID with the graph_run_id printed by the loader.

// 1. Show incoming paths up to five hops toward the root.
MATCH p = (:Address {graph_run_id: "GRAPH_RUN_ID"})-[:TRANSFERRED*1..5]->
          (:Address {graph_run_id: "GRAPH_RUN_ID", is_root: true})
RETURN p;

// 2. Show direct incoming transfers to the root.
MATCH p = (a:Address {graph_run_id: "GRAPH_RUN_ID"})-[r:TRANSFERRED]->
          (root:Address {graph_run_id: "GRAPH_RUN_ID", is_root: true})
RETURN p;

// 3. Show individual transfers worth at least USD 100.
MATCH p = (a:Address {graph_run_id: "GRAPH_RUN_ID"})-[r:TRANSFERRED]->
          (b:Address {graph_run_id: "GRAPH_RUN_ID"})
WHERE r.transfer_value_USD >= 100
RETURN p;

// 4. Inspect transfers whose USD value is unknown (the property is absent/null).
MATCH p = (a:Address {graph_run_id: "GRAPH_RUN_ID"})-[r:TRANSFERRED]->
          (b:Address {graph_run_id: "GRAPH_RUN_ID"})
WHERE r.transfer_value_USD IS NULL
RETURN p;

// 5. Summarize retained transfers by asset.
MATCH (:Address {graph_run_id: "GRAPH_RUN_ID"})-[r:TRANSFERRED]->
      (:Address {graph_run_id: "GRAPH_RUN_ID"})
RETURN r.token_symbol AS asset,
       count(*) AS transfer_count,
       sum(r.transfer_value_USD) AS approximate_usd
ORDER BY approximate_usd DESC;

// 6. Delete exactly one isolated graph run.
MATCH (a:Address {graph_run_id: "GRAPH_RUN_ID"})
DETACH DELETE a;
