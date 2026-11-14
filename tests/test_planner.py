from pantry.agents.planner import Planner
from pantry.llm import FakeLLM


def test_planner_returns_valid_output():
    llm = FakeLLM()
    llm.register(
        "PlannerOutput",
        {
            "dish_name": "Egg Fried Rice",
            "servings": 2,
            "est_minutes": 20,
            "ingredients": [
                {"name": "rice", "amount": 2, "unit": "cups", "estimated": True},
            ],
            "steps": ["Cook the rice.", "Fry and combine."],
            "rationale": "Uses available ingredients.",
        },
    )

    plan = Planner(llm).plan("trigger=STOCK_DEPLETED; have=rice")

    assert plan.dish_name == "Egg Fried Rice"
    assert plan.servings == 2
    assert plan.ingredients[0].name == "rice"

