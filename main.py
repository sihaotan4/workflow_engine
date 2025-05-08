import asyncio
import tomllib
import networkx as nx
from collections import deque
import node_functions
import matplotlib.pyplot as plt
from networkx.drawing.nx_agraph import to_agraph
import pprint


def parse_toml_to_dag(toml_file):
    with open(toml_file, "rb") as file:
        workflow = tomllib.load(file)

    dag = nx.DiGraph()

    # nodes
    for node in workflow["nodes"]:
        node_id = node["node_id"]
        if node_id in dag.nodes:
            raise ValueError(f"Duplicate node_id detected: {node_id}")
        else:
            dag.add_node(node_id, **node)

    # edges
    for node in workflow["nodes"]:
        node_id = node["node_id"]
        for downstream_node in node.get("downstream", []):
            if downstream_node:  # avoid empty downstream entries
                if node_id not in dag.nodes:
                    raise ValueError(
                        f"Cannot add edge from {node_id} to {downstream_node}: {node_id} does not exist."
                    )
                if downstream_node not in dag.nodes:
                    raise ValueError(
                        f"Cannot add edge from {node_id} to {downstream_node}: {downstream_node} does not exist."
                    )
                dag.add_edge(node_id, downstream_node)

    return dag

# TODO: there might be a need for cycles - for e.g. in retry logic
def validate_dag(dag):
    """
    Validates the DAG for the following conditions:
    1. All nodes are connected (the graph is weakly connected).
    2. There are no cycles (the graph is acyclic).
    """

    if not nx.is_weakly_connected(dag):
        raise ValueError("DAG validation failed: Not all nodes are connected.")

    if not nx.is_directed_acyclic_graph(dag):
        raise ValueError("DAG validation failed: The graph contains cycles.")

    print("DAG validation passed: All checks are successful.")


def visualize_dag(dag, output_file):
    # convert the DAG to a Graphviz AGraph
    agraph = to_agraph(dag)
    agraph.graph_attr.update(rankdir="LR")  # Set the direction to Left-to-Right
    agraph.node_attr.update(
        shape="box", style="filled", fillcolor="lightblue"
    )  # Node styling
    agraph.edge_attr.update(color="gray")  # Edge styling

    # render the graph to an SVG file
    agraph.layout("dot")  # use the dot layout engine for hierarchical layout
    agraph.draw(output_file, format="svg")


# node_outputs is a shared dict, but risk of race condition should not exist since node keys 
# are expected to be unique
# furthermore we are in async, so queue state and shared data can be passed around without fear
async def execute_node(node, dag, node_outputs, queue, running_tasks, executed_nodes):
    node_id = node["node_id"]
    node_type = node["node_type"]

    # match node type
    if node_type == "llm_node":
        prompt = node.get("prompt", "")
        predecessors = dag.predecessors(node_id)
        input_data = "\n".join(
            node_outputs.get(predecessor, "").strip() for predecessor in predecessors
        )
        # call the async LLM function
        node_outputs[node_id] = await node_functions.llm_function(
            input_data, prompt, node_id
        )

    elif node_type == "data_fetcher_node":
        source_details = node.get("source_details")
        # call the async data fetching function
        node_outputs[node_id] = await node_functions.data_fetcher_function(
            source_details
        )

    elif node_type == "control_flow_node":
        downstream_nodes = node.get("downstream", [])
        if downstream_nodes:
            # dynamic import of the control flow function (these are unique and not generic fns)
            # imo rarely can control function nodes be generic, there is no reason to force it
            control_flow_function_name = node_id
            control_flow_function = getattr(
                node_functions, control_flow_function_name, None
            )
            if control_flow_function is None:
                raise ValueError(
                    f"Control flow function '{control_flow_function_name}' not found in node_functions.py"
                )

            # call the control flow function to determine which downstream nodes to activate
            predecessors = dag.predecessors(node_id)
            input_data = "\n".join(
                node_outputs.get(predecessor, "").strip()
                for predecessor in predecessors
            )
            selected_nodes = await control_flow_function(input_data, downstream_nodes)

            # store the input data in the control node's entry so the downstream nodes can call on it easily
            node_outputs[node_id] = input_data

            # add selected downstream nodes to the queue
            queue.extend(selected_nodes)

            # add unselected nodes to the executed_nodes set - they will not be run!
            unselected_nodes = set(downstream_nodes) - set(selected_nodes)
            executed_nodes.update(unselected_nodes)

    # queue child nodes (downstream nodes) for the other node types except control_flow_node
    if node_type != "control_flow_node":
        for child_node in dag.successors(node_id):
            # child node has not yet been discovered and thus isn't in these 3 lists
            if (
                child_node not in queue
                and child_node not in executed_nodes
                and child_node not in running_tasks.keys()
            ):
                queue.append(child_node)


def get_dag_roots(dag):
    root_nodes = [node for node, in_degree in dag.in_degree() if in_degree == 0]
    return root_nodes


async def execute_dag(dag):
    root_nodes = get_dag_roots(dag)
    queue = deque(root_nodes)
    executed_nodes = set()
    node_outputs = {}  # shared data structure to track all node outputs, including final output
    running_tasks = {}  # track running tasks by node_id

    # main control loop
    while queue or running_tasks:
        print()
        print(f"QUEUE:     {list(queue)}")
        print(f"RUNNING:   {list(running_tasks.keys())}")
        # print(f'EXECUTED:  {executed_nodes}')
        print()

        # first, check on the status of running tasks
        if running_tasks:
            done, _ = await asyncio.wait(
                running_tasks.values(), return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                try:
                    completed_node_id = next(
                        node_id for node_id, t in running_tasks.items() if t == task
                    )
                    running_tasks.pop(completed_node_id)
                    executed_nodes.add(completed_node_id)
                    print(f"{completed_node_id} - Task completed")
                except Exception as e:
                    print(f"Error processing completed task: {e}")

        # the process the queue
        # important! use a temporary list to defer re-adding nodes to the queue until the end
        # otherwise you will easily end up in infinite loops here
        deferred_nodes = []
        while queue:
            current_node_id = queue.popleft()
            current_node = dag.nodes[current_node_id]

            # check if all predecessors have been executed
            predecessors = list(dag.predecessors(current_node_id))
            if not all(predecessor in executed_nodes for predecessor in predecessors):
                # if predecessors are not complete, defer re-adding the node
                deferred_nodes.append(current_node_id)
                continue

            # otherwise, schedule the execution of the current node on the async runtime
            try:
                task = asyncio.create_task(
                    execute_node(
                        current_node,
                        dag,
                        node_outputs,
                        queue,
                        running_tasks,
                        executed_nodes,
                    )
                )
                running_tasks[current_node_id] = task
                print(f"{current_node_id} - Task started")
            except Exception as e:
                print(f"{current_node_id} - Error scheduling node: {e}")

        # re-add deferred nodes to the queue after processing running tasks
        queue.extend(deferred_nodes)

    print("\nWorkflow completed")
    pprint.pprint(node_outputs, width=400)


def main():
    toml_file = "workflow.toml"
    dag = parse_toml_to_dag(toml_file)
    print("Workflow DAG created")

    # visualize the DAG and save as SVG
    output_file = "workflow_dag.svg"
    visualize_dag(dag, output_file)
    print(f"DAG visualization saved to {output_file}")

    validate_dag(dag)

    asyncio.run(execute_dag(dag))


if __name__ == "__main__":
    main()
