"""Pull all crafting recipes from XIVAPI v2 and save a normalized JSON file.

Each recipe record contains the crafted item, output quantity, crafter job,
required level, unlock book (if any), required Craftsmanship/Control, and
the list of (ingredient_item_id, quantity) pairs for ingredients that are
directly purchasable (base materials), not sub-crafts.
"""
import json
import os
import time
from pathlib import Path

import requests

API_BASE = "https://v2.xivapi.com/api/sheet/Recipe"
FIELDS = ",".join([
    "ItemResult.value",
    "ItemResult.Name",
    "ItemResult.Icon",
    "ItemResult.CanBeHq",
    "AmountResult",
    "Ingredient[].value",
    "Ingredient[].Name",
    "Ingredient[].Icon",
    "Ingredient[].CanBeHq",
    "AmountIngredient",
    "CraftType.Name",
    "RecipeLevelTable.ClassJobLevel",
    "SecretRecipeBook.Name",
    "RequiredCraftsmanship",
    "RequiredControl",
])
PAGE_LIMIT = 500
OUT_PATH = Path(os.environ.get("FFXIV_DATA_DIR", Path(__file__).parent.parent / "data")) / "recipes.json"

# XIVAPI's CraftType sheet uses the old crafting-skill names; map to the
# player-facing crafter job names shown in-game and on GarlandTools.
CRAFT_TYPE_TO_JOB = {
    "Woodworking": "Carpenter",
    "Smithing": "Blacksmith",
    "Armorcraft": "Armorer",
    "Goldsmithing": "Goldsmith",
    "Leatherworking": "Leatherworker",
    "Clothcraft": "Weaver",
    "Alchemy": "Alchemist",
    "Cooking": "Culinarian",
}


def fetch_all_recipes():
    recipes = []
    after = None
    while True:
        params = {"fields": FIELDS, "limit": PAGE_LIMIT}
        if after is not None:
            params["after"] = after
        resp = requests.get(API_BASE, params=params, timeout=30)
        resp.raise_for_status()
        payload = resp.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
            raise ValueError("Malformed recipe page; existing recipes were preserved")
        rows = payload["rows"]
        if not rows:
            break

        for row in rows:
            f = row["fields"]
            result_id = f["ItemResult"]["value"]
            if result_id == 0:
                continue  # unused recipe slot

            ingredients = []
            for ing, qty in zip(f["Ingredient"], f["AmountIngredient"]):
                if ing["value"] > 0 and qty > 0:
                    ingredients.append({
                        "item_id": ing["value"],
                        "name": ing["fields"]["Name"],
                        "icon_path": ing["fields"]["Icon"]["path"],
                        "quantity": qty,
                        "can_be_hq": ing["fields"].get("CanBeHq"),
                    })
            if not ingredients:
                continue

            craft_type_name = f["CraftType"]["fields"].get("Name", "")
            book_name = (f.get("SecretRecipeBook") or {}).get("fields", {}).get("Name", "")

            recipes.append({
                "recipe_id": row["row_id"],
                "result_item_id": result_id,
                "result_name": f["ItemResult"]["fields"]["Name"],
                "icon_path": f["ItemResult"]["fields"]["Icon"]["path"],
                "amount_result": f["AmountResult"],
                "can_be_hq": f["ItemResult"]["fields"].get("CanBeHq"),
                "craft_type": craft_type_name,
                "crafter_job": CRAFT_TYPE_TO_JOB.get(craft_type_name, craft_type_name),
                "level": (f.get("RecipeLevelTable") or {}).get("fields", {}).get("ClassJobLevel", 0),
                "unlock_item": book_name or None,
                "required_craftsmanship": f.get("RequiredCraftsmanship", 0),
                "required_control": f.get("RequiredControl", 0),
                "ingredients": ingredients,
            })

        next_after = rows[-1]["row_id"]
        if after is not None and next_after <= after:
            raise ValueError("Recipe pagination did not advance")
        after = next_after
        print(f"  fetched through row {after}, {len(recipes)} recipes so far")
        if len(rows) < PAGE_LIMIT:
            break
        time.sleep(0.1)

    return recipes


def main():
    print("Fetching all recipes from XIVAPI v2...")
    recipes = fetch_all_recipes()
    if not recipes:
        raise ValueError("No recipes returned; existing recipes were preserved")
    OUT_PATH.parent.mkdir(exist_ok=True)
    temporary = OUT_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(recipes, indent=2))
    temporary.replace(OUT_PATH)
    print(f"Saved {len(recipes)} recipes to {OUT_PATH}")


if __name__ == "__main__":
    main()
