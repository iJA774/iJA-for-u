def main(data):
    items = data.get("items", [])
    result = {}
    for item in items:
        owner = item.get("owner", "未分配")
        task = item.get("task", "")
        done = item.get("done", False)
        if owner not in result:
            result[owner] = {
                "total": 0,
                "completed": 0,
                "pending_tasks": []
            }
        result[owner]["total"] += 1
        if done:
            result[owner]["completed"] += 1
        else:
            result[owner]["pending_tasks"].append(task)
    return result
