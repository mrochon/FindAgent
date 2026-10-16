import azure.functions as func

from agent_indexer.run import Settings, run_indexer

app = func.FunctionApp()


@app.timer_trigger(schedule="%INDEXER_SCHEDULE%", arg_name="timer", run_on_startup=False, use_monitor=True)
def index_agents(timer: func.TimerRequest) -> None:
    run_indexer(Settings.from_env())
