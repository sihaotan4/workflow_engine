import asyncio
import random


## llm call function
async def llm_function(data: str, prompt: str, node_id: str) -> str:
    await asyncio.sleep(
        random.uniform(1, 5)
    )  # simulate random processing time between 1 and 5 seconds
    llm_output = f"{node_id}({data})"  # simulate answer
    return llm_output


## data fetcher function
async def data_fetcher_function(source_details) -> str:
    await asyncio.sleep(
        random.uniform(1, 5)
    )  # simulate random processing time between 1 and 5 seconds
    data = f"{source_details}"  # simulate some data
    return data


async def control_flow_data_source_a(data, downstream_node_ids):
    await asyncio.sleep(
        random.uniform(0.1, 1)
    )  # simulate random processing time between 0.1 and 1 second
    selected_nodes = random.sample(
        downstream_node_ids, random.randint(0, len(downstream_node_ids))
    )  # simulate some form of control flow logic based on the data input
    return selected_nodes
