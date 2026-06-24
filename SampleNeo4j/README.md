# Sample Neo4j Graph Visualization

This folder contains a minimal Neo4j visualization workflow:

- `load_sample_graph.py` writes a small sample graph to Neo4j.
- `export_sample_graph_html.py` reads that graph back and exports an interactive PyVis HTML file.
- `run_sample_neo4j_graph.sh` creates a Python virtual environment, installs dependencies, runs both Python scripts, and optionally opens the HTML file on macOS.

## 1. Install Neo4j on macOS

### Option A: Homebrew service

```bash
brew install neo4j
brew services start neo4j
brew services list
```

Then open Neo4j Browser at <http://localhost:7474> and set/change the password when prompted.

### Option B: Neo4j Desktop

```bash
brew install --cask neo4j-desktop
open -a "Neo4j Desktop"
```

Create a local DBMS in Neo4j Desktop and start it.

### Option C: Docker

```bash
docker run --name sample-neo4j \
  --publish=7474:7474 \
  --publish=7687:7687 \
  --env NEO4J_AUTH=neo4j/password123 \
  --volume="$HOME/neo4j/data:/data" \
  neo4j:latest
```

## 2. Run the sample visualization

From the repository root:

```bash
export NEO4J_PASSWORD='your-neo4j-password'
bash SampleNeo4j/run_sample_neo4j_graph.sh
```

The script writes `SampleNeo4j/sample_neo4j_graph.html` by default. On macOS it opens the file automatically unless you pass `--no-open`.

## 3. View the graph inside Neo4j Browser

Open <http://localhost:7474>, log in, and run:

```cypher
MATCH p = (:SampleDemo)-[*1..2]->(:SampleDemo)
RETURN p
LIMIT 100;
```

## Useful options

```bash
bash SampleNeo4j/run_sample_neo4j_graph.sh --no-open
bash SampleNeo4j/run_sample_neo4j_graph.sh --output /tmp/sample_neo4j_graph.html
bash SampleNeo4j/run_sample_neo4j_graph.sh --keep-existing
```

You can also override connection settings:

```bash
export NEO4J_URI='bolt://localhost:7687'
export NEO4J_USER='neo4j'
export NEO4J_DATABASE='neo4j'
export NEO4J_PASSWORD='your-neo4j-password'
bash SampleNeo4j/run_sample_neo4j_graph.sh
```
