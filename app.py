from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash, Response
import datetime
import hmac
import os

from recipe_db import RecipeDB, single_plural_for
from sheets import get_spreadsheet

app = Flask(__name__)
# Geheime sleutel voor sessies: altijd via omgevingsvariabele (zie .env.example)
app.secret_key = os.environ.get("SECRET_KEY") or os.urandom(32)


#-------------------------SECTION 0 OPTIONELE BEVEILIGING ----------------------------------------------------------
# Als APP_PASSWORD gezet is, vraagt de browser om gebruikersnaam/wachtwoord (Basic Auth).
# Gebruikersnaam = APP_USER (default "recepten"). Zonder APP_PASSWORD is de app open,
# bijvoorbeeld als de toegang al door Cloudflare Access / Coolify geregeld wordt.
APP_USER = os.environ.get("APP_USER", "recepten")
APP_PASSWORD = os.environ.get("APP_PASSWORD")


@app.before_request
def require_login():
    if not APP_PASSWORD or request.path == "/health":
        return None
    auth = request.authorization
    if auth and hmac.compare_digest(auth.username or "", APP_USER) \
            and hmac.compare_digest(auth.password or "", APP_PASSWORD):
        return None
    return Response("Inloggen vereist", 401, {"WWW-Authenticate": 'Basic realm="Recepten"'})


@app.route('/health')
def health():
    return "ok"


#-------------------------SECTION 1 CONNECT TO GOOGLE SHEETS ----------------------------------------------------------
class _Client:
    """Dunne wrapper zodat bestaande code `client.open("Recepten")` blijft werken.
    Geeft de gedeelde, gecachte spreadsheet terug (zie sheets.py): één keer openen,
    alle tabbladen in één aanroep ophalen en een paar minuten bewaren."""
    def open(self, _name=None):
        return get_spreadsheet()


def get_gsheet():
    return _Client()


def _db():
    """Veilige schrijfhulp: alleen eigen kolommen, id's als tekst, formules in nieuwe rijen overnemen."""
    return RecipeDB(get_spreadsheet())


# Endpoints met POST die niets naar de sheet schrijven (alleen weekkeuze)
_READ_ONLY_POSTS = {"weekly_recipes"}


@app.before_request
def fresh_data_for_writes():
    """Voor elke schrijfactie verse gegevens ophalen, zodat nieuwe Keys en
    rijnummers nooit op een verouderde cache gebaseerd zijn."""
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.endpoint not in _READ_ONLY_POSTS:
        get_spreadsheet().invalidate()


def _remark(line):
    """Lines-kolom heet in de sheet 'ArticleRemark' of 'Remark' (DB_structuur) – beide ondersteunen."""
    return line.get('ArticleRemark') or line.get('Remark') or ''


def _week_dates(week):
    start = datetime.date.fromisocalendar(datetime.date.today().year, int(week), 1)
    end = start + datetime.timedelta(days=6)
    return start.strftime("%d-%b"), end.strftime("%d-%b")


#-------------------------SECTION 2 HOME ----------------------------------------------------------
@app.route('/')
def home():
    # Clear selected_week when navigating to the home page
    session.pop('selected_week', None)
    session.pop('selected_recipe_id', None)
    return render_template('home.html')


#-------------------------SECTION 3 WEEKMENU ----------------------------------------------------------
#------Section 3.1: Display recipe per week with status------
@app.route('/weekly_recipes', methods=['GET', 'POST'])
def weekly_recipes():
    client = get_gsheet()
    jaar_sheet = client.open("Recepten").worksheet("Jaar")
    status_sheet = client.open("Recepten").worksheet("Week_status")

    current_week = datetime.date.today().isocalendar()[1]

    if request.method == 'POST':
        selected_week = int(request.form['selected_week'])
        session['selected_week'] = selected_week
    else:
        selected_week = session.get('selected_week', current_week)

    status_data = status_sheet.get_all_records()
    week_status = next((row['Status'] for row in status_data if row['Week'] == selected_week), 'not_approved')

    jaar_data = jaar_sheet.get_all_records()
    header_data = client.open("Recepten").worksheet("RecipeHeader").get_all_records()
    main_data = client.open("Recepten").worksheet("RecipeMain").get_all_records()

    weekly_recipes = []
    for row in jaar_data:
        if row['Week'] == selected_week and row.get('RecipeId'):
            recipe_id = row['RecipeId']
            recipe_header = next((rec for rec in header_data if rec['RecipeId'] == recipe_id), None)
            recipe_main = next((rec for rec in main_data if rec['RecipeId'] == recipe_id), None)
            if recipe_header and recipe_main:
                row['Link'] = recipe_header.get('Link', '')
                row['Title'] = recipe_main.get('Title', '')
                weekly_recipes.append(row)

    start_date_formatted, end_date_formatted = _week_dates(selected_week)
    weekly_count = sum(1 for row in jaar_data if row.get('Week') == selected_week and row.get('RecipeId'))

    return render_template(
        'weekly_recipes.html',
        current_week=current_week,
        selected_week=selected_week,
        prev_week=current_week - 1,
        next_week_1=current_week + 1,
        next_week_2=current_week + 2,
        weekly_recipes=weekly_recipes,
        weekly_count=weekly_count,
        start_date=start_date_formatted,
        end_date=end_date_formatted,
        week_status=week_status
    )


#------Section 3.2: Delete recipe (from weekmenu)------
@app.route('/delete_recipe', methods=['POST'])
def delete_recipe():
    client = get_gsheet()
    jaar_sheet = client.open("Recepten").worksheet("Jaar")

    recipe_id = request.form['RecipeId']
    unique_key = request.form['Key']

    jaar_data = jaar_sheet.get_all_records()
    for i, row in enumerate(jaar_data):
        if str(row.get('Key')) == str(unique_key).strip():
            # RecipeId leegmaken (kolom D), rij blijft bestaan
            jaar_sheet.update_cell(i + 2, 4, '')
            print(f"Deleted RecipeId {recipe_id} from row {i + 2} with Key {unique_key}")
            break

    return redirect(url_for('weekly_recipes'))


#------Section 3.3: Add recipe to weekly menu------
#------3.3A GET-----------
@app.route('/add_recipe_to_weekmenu', methods=['GET'])
def add_recipe_to_weekmenu():
    client = get_gsheet()
    main_sheet = client.open("Recepten").worksheet("RecipeMain")
    lines_sheet = client.open("Recepten").worksheet("Lines")

    selected_week = session.get('selected_week', datetime.date.today().isocalendar()[1])

    all_recipes = main_sheet.get_all_records()
    titles = [(r['Title'], r['RecipeId']) for r in all_recipes]

    lines_data = lines_sheet.get_all_records()
    lines = list({row['ArticleId'] for row in lines_data if row.get('ArticleId')})

    selected_recipe = None
    selected_recipe_id = request.args.get('RecipeId')

    if selected_recipe_id:
        selected_recipe = next((r for r in all_recipes if str(r['RecipeId']) == selected_recipe_id), None)
        if selected_recipe:
            session['selected_recipe_id'] = selected_recipe_id
            session['selected_recipe'] = selected_recipe

    return render_template(
        'add_recipe_to_weekmenu.html',
        selected_week=selected_week,
        titles=titles,
        lines=lines,
        selected_recipe=selected_recipe,
        selected_recipe_id=selected_recipe_id
    )


#------3.3B POST-----------
@app.route('/add_to_weekmenu', methods=['POST'])
def add_to_weekmenu():
    recipe_id = request.form.get('RecipeId')
    week = request.form.get('Week')

    if not recipe_id or not week:
        return "RecipeId en Week zijn verplicht", 400

    week = int(week)
    client = get_gsheet()
    jaar_sheet = client.open("Recepten").worksheet("Jaar")
    jaar_data = jaar_sheet.get_all_records()

    for i, row in enumerate(jaar_data):
        if row.get('Week') == week and not row.get('RecipeId'):
            jaar_sheet.update_cell(i + 2, 4, int(recipe_id) if str(recipe_id).isdigit() else recipe_id)
            print(f"✅ Recipe {recipe_id} toegevoegd aan week {week}, rij {i + 2}")
            break
    else:
        return f"❌ Geen lege rij gevonden voor week {week}", 400

    return redirect(url_for('weekly_recipes'))


#------Section 3.3C  -----    #Artikel selectiemethode
@app.route('/list_article_names')
def list_article_names():
    client = get_gsheet()
    articles_data = client.open("Recepten").worksheet("Articles").get_all_records()

    result = [
        {"ArticleId": row["ArticleId"], "NameSingle": row["NameSingle"]}
        for row in articles_data
        if row.get("ArticleId") and row.get("NameSingle")
    ]
    result.sort(key=lambda x: x["NameSingle"].lower())
    return jsonify(result)


@app.route('/fetch_recipes_by_article')
def fetch_recipes_by_article():
    name_single = request.args.get('NameSingle')
    if not name_single:
        return jsonify([])

    client = get_gsheet()
    articles_data = client.open("Recepten").worksheet("Articles").get_all_records()
    lines_data = client.open("Recepten").worksheet("Lines").get_all_records()
    recipes_data = client.open("Recepten").worksheet("RecipeMain").get_all_records()

    article_row = next((row for row in articles_data if row.get('NameSingle') == name_single), None)
    if not article_row:
        return jsonify([])

    article_id = str(article_row['ArticleId'])
    matching_recipe_ids = {
        str(line['RecipeId'])
        for line in lines_data
        if str(line.get('ArticleId')) == article_id
    }
    matching_recipes = [
        {'RecipeId': recipe['RecipeId'], 'Title': recipe['Title']}
        for recipe in recipes_data
        if str(recipe['RecipeId']) in matching_recipe_ids
    ]
    return jsonify(matching_recipes)


#------Section 3.3D  -----    #Title selectiemethode
@app.route('/list_recipe_titles')
def list_recipe_titles():
    client = get_gsheet()
    recipes_data = client.open("Recepten").worksheet("RecipeMain").get_all_records()
    titles = [
        {"RecipeId": row["RecipeId"], "Title": row["Title"]}
        for row in recipes_data if row.get("Title")
    ]
    titles.sort(key=lambda x: x["Title"].lower())
    return jsonify(titles)


@app.route('/fetch_recipe_by_title')
def fetch_recipe_by_title():
    title = request.args.get('Title')
    if not title:
        return jsonify({})

    client = get_gsheet()
    main_data = client.open("Recepten").worksheet("RecipeMain").get_all_records()
    header_data = client.open("Recepten").worksheet("RecipeHeader").get_all_records()

    recipe_main = next((row for row in main_data if row.get('Title') == title), None)
    if not recipe_main:
        return jsonify({})

    recipe_id = recipe_main['RecipeId']
    recipe_header = next((row for row in header_data if row['RecipeId'] == recipe_id), None)
    link = recipe_header['Link'] if recipe_header else ''

    return jsonify({"RecipeId": recipe_id, "Title": recipe_main['Title'], "Link": link})


#------Section 3.4: Display the contents of the recipe -----
@app.route('/get_recipe_details/<selected_recipe_id>')
def get_recipe_details(selected_recipe_id):
    # Oude route; de volledige weergave zit in /select_recipe/<id>
    return redirect(url_for('select_recipe', recipe_id=selected_recipe_id))


#------Section 3.6: Toggle Week Status------
@app.route('/toggle_status', methods=['POST'])
def toggle_status():
    client = get_gsheet()
    status_sheet = client.open("Recepten").worksheet("Week_status")

    selected_week = int(request.form['week'])
    status_data = status_sheet.get_all_records()

    for i, row in enumerate(status_data):
        if row['Week'] == selected_week:
            new_status = 'approved' if row['Status'] == 'not_approved' else 'not_approved'
            status_sheet.update_cell(i + 2, 3, new_status)
            break

    return redirect(url_for('weekly_recipes'))


#-------------------------SECTION 4 ADD RECIPE (to database)  ----------------------------------------------------------
@app.route('/add', methods=['GET', 'POST'])
def add_recipe():
    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        if not title:
            return "Title is required", 400

        try:
            client = get_gsheet()
            recipe_main_sheet = client.open("Recepten").worksheet("RecipeMain")
            all_records = recipe_main_sheet.get_all_records()

            max_key = max((int(r['Key']) for r in all_records if str(r.get('Key', '')).strip().isdigit()), default=0)
            max_recipe_id = max((int(r['RecipeId']) for r in all_records if str(r.get('RecipeId', '')).strip().isdigit()), default=0)
            new_key, new_recipe_id = max_key + 1, max_recipe_id + 1

            _db().append_rows("RecipeMain", [{'Key': new_key, 'RecipeId': new_recipe_id, 'Title': title}])
            print(f"✅ Recipe '{title}' added with Key: {new_key}, RecipeId: {new_recipe_id}")
            return redirect(url_for('add_ingredients', recipe_id=new_recipe_id))

        except Exception as e:
            print(f"❌ Error adding recipe: {str(e)}")
            return f"Error adding recipe: {str(e)}", 500

    return render_template('add_recipe.html')


#-------------------------SECTION 4B ADD INGREDIENTS TO LINES SHEET -------------------------------------------------------------
@app.route('/add_ingredients/<int:recipe_id>', methods=['GET', 'POST'])
def add_ingredients(recipe_id):
    if request.method == 'POST':
        try:
            ingredients_data = []
            i = 1
            while True:
                qty_key = f'qty_{i}'
                # Regels kunnen in de browser verwijderd zijn → nummering kan gaten hebben.
                # Stop pas als er een ruime reeks ontbrekende nummers is.
                if qty_key not in request.form:
                    if not any(f'qty_{j}' in request.form for j in range(i, i + 50)):
                        break
                    i += 1
                    continue

                qty = request.form.get(qty_key, '').strip()
                uom = request.form.get(f'uom_{i}', '').strip()
                article = request.form.get(f'article_{i}', '').strip()
                remark = request.form.get(f'remark_{i}', '').strip()

                if not qty and not uom and not article:
                    i += 1
                    continue
                if not qty:
                    return f"Qty is required for ingredient line {i}", 400
                try:
                    qty_float = float(qty)
                    if '.' in qty and len(qty.split('.')[1]) > 1:
                        return f"Qty can only have 1 decimal place (line {i})", 400
                except ValueError:
                    return f"Qty must be a number (line {i})", 400
                if len(remark) > 20:
                    return f"ArticleRemark must be 20 characters or less (line {i})", 400

                ingredients_data.append({
                    'qty': qty_float if qty_float != int(qty_float) else int(qty_float),
                    'uom': uom, 'article': article, 'remark': remark,
                })
                i += 1

            if not ingredients_data:
                return "At least one ingredient is required", 400

            client = get_gsheet()
            lines_sheet = client.open("Recepten").worksheet("Lines")
            uom_data = client.open("Recepten").worksheet("Uom").get_all_records()
            articles_data = client.open("Recepten").worksheet("Articles").get_all_records()

            all_lines = lines_sheet.get_all_records()
            next_key = max((int(l['Key']) for l in all_lines if str(l.get('Key', '')).strip().isdigit()), default=0) + 1
            # LineId is uniek per recept: verder tellen vanaf het hoogste LineId van dit recept
            existing_line_ids = [int(l['LineId']) for l in all_lines
                                 if str(l.get('RecipeId')) == str(recipe_id) and str(l.get('LineId', '')).strip().isdigit()]
            line_id = max(existing_line_ids, default=0) + 1

            uom_name_to_id = {row['UomName']: row['UomId'] for row in uom_data if row.get('UomName')}
            article_single = {row['NameSingle']: row for row in articles_data if row.get('NameSingle')}
            article_plural = {row['NamePlural']: row for row in articles_data if row.get('NamePlural')}

            headers = lines_sheet.row_values(1)
            rows = []
            for ing in ingredients_data:
                uom_id = uom_name_to_id.get(ing['uom'])
                if not uom_id:
                    return f"Invalid UOM selected: {ing['uom']}", 400
                art = article_single.get(ing['article']) or article_plural.get(ing['article'])
                if not art or not art.get('ArticleId'):
                    return f"Invalid Article selected: {ing['article']}", 400

                single_plural = single_plural_for(ing['qty'], ing['uom'])
                display = art['NameSingle'] if single_plural == 'SINGLE' else art['NamePlural']
                line_data_dict = {
                    'Key': next_key, 'RecipeId': recipe_id, 'LineId': line_id,
                    'Qty': ing['qty'], 'UomId': uom_id, 'ArticleId': art['ArticleId'],
                    'Article': display, 'ArticleRemark': ing['remark'], 'Remark': ing['remark'],
                    'SinglePlural': single_plural,
                }
                rows.append(line_data_dict)
                next_key += 1
                line_id += 1

            _db().append_rows("Lines", rows)   # één API-aanroep; formules en tekst-id's correct
            print(f"✅ Added {len(rows)} ingredients for RecipeId {recipe_id}")
            return redirect(url_for('add_recipe_header', recipe_id=recipe_id))

        except Exception as e:
            print(f"❌ Error adding ingredients: {str(e)}")
            return f"Error adding ingredients: {str(e)}", 500

    # GET request - show the form
    try:
        client = get_gsheet()
        uom_data = client.open("Recepten").worksheet("Uom").get_all_records()
        articles_data = client.open("Recepten").worksheet("Articles").get_all_records()
        recipe_data = client.open("Recepten").worksheet("RecipeMain").get_all_records()

        recipe = next((r for r in recipe_data if r['RecipeId'] == recipe_id), None)
        if not recipe:
            return "Recipe not found", 404

        articles_data = sorted(articles_data, key=lambda a: str(a.get('NameSingle', '')).lower())
        return render_template(
            'add_ingredients.html',
            recipe_id=recipe_id,
            recipe_title=recipe['Title'],
            uom_data=uom_data,
            articles_data=articles_data
        )
    except Exception as e:
        return f"Error loading form: {str(e)}", 500


# API endpoint to get UOM options
@app.route('/api/get_uom_options')
def get_uom_options():
    try:
        uom_data = get_gsheet().open("Recepten").worksheet("Uom").get_all_records()
        options = [{'value': row['UomName'], 'text': row['UomName']} for row in uom_data if row.get('UomName')]
        options.sort(key=lambda x: x['text'].lower())
        return jsonify(options)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# API endpoint to get Article options based on quantity
@app.route('/api/get_article_options')
def get_article_options():
    try:
        qty_float = float(request.args.get('qty', '1'))
        articles_data = get_gsheet().open("Recepten").worksheet("Articles").get_all_records()
        col = 'NameSingle' if qty_float <= 1.0 else 'NamePlural'
        options = [{'value': row[col], 'text': row[col]} for row in articles_data if row.get(col)]
        options.sort(key=lambda x: x['text'].lower())
        return jsonify(options)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


#-------------------------SECTION 4C ADD RECIPE HEADER INFORMATION ----------------------------------------------------------
@app.route('/add_recipe_header/<int:recipe_id>', methods=['GET', 'POST'])
def add_recipe_header(recipe_id):
    if request.method == 'POST':
        try:
            persons = request.form.get('persons', '').strip()
            if len(persons) > 10:
                return "Persons field must be 10 characters or less", 400

            client = get_gsheet()
            header_sheet = client.open("Recepten").worksheet("RecipeHeader")
            recipe_data = client.open("Recepten").worksheet("RecipeMain").get_all_records()

            if not next((r for r in recipe_data if r['RecipeId'] == recipe_id), None):
                return "Recipe not found", 404

            all_headers = header_sheet.get_all_records()
            if next((h for h in all_headers if h.get('RecipeId') == recipe_id), None):
                return f"Recipe header already exists for RecipeId {recipe_id}", 400
            next_key = max((int(h['Key']) for h in all_headers if str(h.get('Key', '')).strip().isdigit()), default=0) + 1

            header_data_dict = {
                'Key': next_key,
                'RecipeId': recipe_id,
                'Link': request.form.get('link', '').strip(),
                'Content': request.form.get('content', '').strip(),
                'Remarks': request.form.get('remarks', '').strip(),
                'Utensils': request.form.get('utensils', '').strip(),
                'CookingTime': request.form.get('cooking_time', '').strip(),
                'Persons': persons,
            }
            headers = header_sheet.row_values(1)
            _db().append_rows("RecipeHeader", [header_data_dict])
            print(f"✅ Added recipe header with Key {next_key} for RecipeId {recipe_id}")
            return redirect(url_for('select_recipe', recipe_id=recipe_id))

        except Exception as e:
            print(f"❌ Error adding recipe header: {str(e)}")
            return f"Error adding recipe header: {str(e)}", 500

    try:
        recipe_data = get_gsheet().open("Recepten").worksheet("RecipeMain").get_all_records()
        recipe = next((r for r in recipe_data if r['RecipeId'] == recipe_id), None)
        if not recipe:
            return "Recipe not found", 404
        return render_template('add_recipe_header.html', recipe_id=recipe_id, recipe_title=recipe['Title'])
    except Exception as e:
        return f"Error loading form: {str(e)}", 500


#-------------------------SECTION 5 DISPLAY RECIPE ----------------------------------------------------------
# Stateless route via URL parameter – ook gebruikt in iframes
@app.route('/select_recipe/<recipe_id>')
def select_recipe(recipe_id):
    session['selected_recipe_id'] = recipe_id

    client = get_gsheet()
    main_data = client.open("Recepten").worksheet("RecipeMain").get_all_records()
    selected_recipe = next((r for r in main_data if str(r['RecipeId']) == str(recipe_id)), None)
    if not selected_recipe:
        return "Recipe not found.", 404

    header_data = client.open("Recepten").worksheet("RecipeHeader").get_all_records()
    # Een recept zonder header (bijv. halverwege toevoegen gestopt) toont gewoon zonder extra info
    selected_header = next((h for h in header_data if str(h['RecipeId']) == str(recipe_id)), {})

    lines_data = client.open("Recepten").worksheet("Lines").get_all_records()
    recipe_lines = sorted(
        [l for l in lines_data if str(l['RecipeId']) == str(recipe_id)],
        key=lambda l: int(l['LineId']) if str(l.get('LineId', '')).isdigit() else 0,
    )

    articles_data = client.open("Recepten").worksheet("Articles").get_all_records()
    article_lookup = {str(a['ArticleId']): a for a in articles_data if a.get('ArticleId')}
    uom_data = client.open("Recepten").worksheet("Uom").get_all_records()
    uom_lookup = {str(u['UomId']): u for u in uom_data if u.get('UomId')}

    detailed_lines = []
    for line in recipe_lines:
        article = article_lookup.get(str(line.get('ArticleId', '')).strip(), {})
        name_col = 'NameSingle' if line.get('SinglePlural') == 'SINGLE' else 'NamePlural'
        detailed_lines.append({
            **line,
            "ArticleName": article.get(name_col, '') or line.get('Article', ''),
            "UomName": uom_lookup.get(str(line.get('UomId', '')).strip(), {}).get('UomName', ''),
            "ArticleRemark": _remark(line),
        })

    selected_week = session.get('selected_week')
    if selected_week:
        start_date_formatted, end_date_formatted = _week_dates(selected_week)
    else:
        start_date_formatted = end_date_formatted = ''

    return render_template(
        'selected_recipe.html',
        recipe=selected_recipe,
        header=selected_header,
        lines=detailed_lines,
        selected_week=selected_week,
        current_week=datetime.date.today().isocalendar()[1],
        start_date=start_date_formatted,
        end_date=end_date_formatted
    )


# Stateful route via session – stuurt door naar de stateless variant
@app.route('/selected_recipe')
def selected_recipe():
    selected_recipe_id = session.get('selected_recipe_id')
    if selected_recipe_id is None:
        return "Recipe ID is missing", 400
    return redirect(url_for('select_recipe', recipe_id=selected_recipe_id))


#-------------------------SECTION 6 INGREDIENTS ----------------------------------------------------------
@app.route('/ingredients', methods=['GET', 'POST'])
def ingredients():
    client = get_gsheet()
    jaar_sheet = client.open("Recepten").worksheet("Jaar")
    main_sheet = client.open("Recepten").worksheet("RecipeMain")
    lines_sheet = client.open("Recepten").worksheet("Lines")
    status_sheet = client.open("Recepten").worksheet("Week_status")
    articles_sheet = client.open("Recepten").worksheet("Articles")
    uom_sheet = client.open("Recepten").worksheet("Uom")

    current_week = datetime.date.today().isocalendar()[1]
    if request.method == 'POST':
        selected_week = int(request.form['selected_week'])
        session['selected_week'] = selected_week
    else:
        selected_week = session.get('selected_week', current_week)

    status_data = status_sheet.get_all_records()
    week_status = next((row['Status'] for row in status_data if row['Week'] == selected_week), 'not_approved')
    start_date_formatted, end_date_formatted = _week_dates(selected_week)

    jaar_data = jaar_sheet.get_all_records()
    recipe_ids_for_week = [str(row['RecipeId']) for row in jaar_data if row.get('Week') == selected_week and row.get('RecipeId')]

    main_data = main_sheet.get_all_records()
    recipe_titles = {str(row['RecipeId']): row['Title'] for row in main_data}

    lines_data = lines_sheet.get_all_records()
    article_lookup = {
        str(a['ArticleId']): {'NameSingle': a.get('NameSingle', ''), 'NamePlural': a.get('NamePlural', '')}
        for a in articles_sheet.get_all_records() if a.get('ArticleId')
    }
    uom_lookup = {str(r["UomId"]): r["UomName"] for r in uom_sheet.get_all_records() if r.get("UomId") and r.get("UomName")}

    ingredients_list = []
    for recipe_id in recipe_ids_for_week:
        recipe_title = recipe_titles.get(recipe_id, '')
        for line in lines_data:
            if str(line['RecipeId']) != recipe_id:
                continue
            article_info = article_lookup.get(str(line.get('ArticleId', '')).strip(), {})
            if line.get('SinglePlural', 'PLURAL') == 'SINGLE':
                article_name = article_info.get('NameSingle', 'Onbekend')
            else:
                article_name = article_info.get('NamePlural', 'Onbekend')

            ingredients_list.append({
                "Title": recipe_title,
                "RecipeId": recipe_id,
                "ArticleId": str(line.get('ArticleId', '')).strip(),
                "NamePlural": article_name,
                # Template gebruikt nog de naam 'UomNamePlural'; inhoud is gewoon UomName
                "UomNamePlural": uom_lookup.get(str(line.get('UomId', '')).strip(), ''),
                "Qty": line.get("Qty", ""),
                "ArticleRemark": _remark(line),
            })

    return render_template(
        'ingredients.html',
        current_week=current_week,
        selected_week=selected_week,
        prev_week=current_week - 1,
        next_week_1=current_week + 1,
        next_week_2=current_week + 2,
        start_date=start_date_formatted,
        end_date=end_date_formatted,
        week_status=week_status,
        ingredients_list=ingredients_list
    )


#-------------------------SECTION 7 RECEPT BEWERKEN ----------------------------------------------------------

# ── 7.0  GET: Receptenlijst – kies recept om te bewerken ─────────────────────
@app.route('/recipe_list')
def recipe_list():
    main_sheet = get_gsheet().open("Recepten").worksheet("RecipeMain")
    recipes = sorted(main_sheet.get_all_records(), key=lambda r: str(r['Title']).lower())
    return render_template('recipe_list.html', recipes=recipes)


# ── 7.1  GET: Toon het bewerkscherm ──────────────────────────────────────────
@app.route('/edit_recipe/<int:recipe_id>', methods=['GET'])
def edit_recipe(recipe_id):
    client = get_gsheet()
    main_data = client.open("Recepten").worksheet("RecipeMain").get_all_records()
    recipe = next((r for r in main_data if r['RecipeId'] == recipe_id), None)
    if not recipe:
        return "Recept niet gevonden", 404

    header_data = client.open("Recepten").worksheet("RecipeHeader").get_all_records()
    header = next((r for r in header_data if r['RecipeId'] == recipe_id), {})

    lines_data = client.open("Recepten").worksheet("Lines").get_all_records()
    recipe_lines = sorted(
        [l for l in lines_data if str(l['RecipeId']) == str(recipe_id)],
        key=lambda l: int(l['LineId']) if str(l.get('LineId', '')).isdigit() else 0,
    )

    articles_data = client.open("Recepten").worksheet("Articles").get_all_records()
    article_lookup = {str(a['ArticleId']): a for a in articles_data if a.get('ArticleId')}
    uom_data = client.open("Recepten").worksheet("Uom").get_all_records()
    uom_lookup = {str(u['UomId']): u for u in uom_data if u.get('UomId')}

    detailed_lines = []
    for line in recipe_lines:
        article = article_lookup.get(str(line.get('ArticleId', '')).strip(), {})
        uom = uom_lookup.get(str(line.get('UomId', '')).strip(), {})
        detailed_lines.append({
            **line,
            'ArticleId': line.get('ArticleId', ''),
            'UomId': line.get('UomId', ''),
            'ArticleName': article.get('NameSingle', ''),
            'UomName': uom.get('UomName', ''),
            'ArticleRemark': _remark(line),
        })

    all_articles = sorted(
        [a for a in articles_data if a.get('ArticleId') and a.get('NameSingle')],
        key=lambda a: str(a['NameSingle']).lower()
    )
    all_uoms = sorted(
        [u for u in uom_data if u.get('UomId') and u.get('UomName')],
        key=lambda u: str(u.get('UomName', '')).lower()
    )

    return render_template(
        'edit_recipe.html',
        recipe=recipe,
        header=header,
        lines=detailed_lines,
        all_articles=all_articles,
        all_uoms=all_uoms,
    )


def _update_row(sheet_name, row_number, changes):
    """Schrijf alleen de gewijzigde cellen (formules en tekst-id's blijven intact)."""
    _db().update_cells(sheet_name, [{"row": row_number, "values": changes}])


def _col_letter(n):
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


# ── 7.2  POST: Sla algemene receptgegevens op (RecipeMain + RecipeHeader) ────
@app.route('/edit_recipe_main/<int:recipe_id>', methods=['POST'])
def edit_recipe_main(recipe_id):
    client = get_gsheet()
    main_sheet = client.open("Recepten").worksheet("RecipeMain")
    header_sheet = client.open("Recepten").worksheet("RecipeHeader")

    new_title = request.form.get('Title', '').strip()
    new_link = request.form.get('Link', '').strip()
    new_content = request.form.get('Content', '').strip()

    main_data = main_sheet.get_all_records()
    for i, row in enumerate(main_data):
        if row['RecipeId'] == recipe_id:
            _update_row("RecipeMain", i + 2, {'Title': new_title})
            break

    header_data = header_sheet.get_all_records()
    for i, row in enumerate(header_data):
        if row['RecipeId'] == recipe_id:
            _update_row("RecipeHeader", i + 2, {'Link': new_link, 'Content': new_content})
            break
    else:
        # Nog geen header voor dit recept → aanmaken
        next_key = max((int(h['Key']) for h in header_data if str(h.get('Key', '')).strip().isdigit()), default=0) + 1
        new = {'Key': next_key, 'RecipeId': recipe_id, 'Link': new_link, 'Content': new_content}
        _db().append_rows("RecipeHeader", [new])

    flash('Algemene gegevens opgeslagen.', 'success')
    return redirect(url_for('edit_recipe', recipe_id=recipe_id))


# ── 7.3  POST: Sla overige headergegevens op (RecipeHeader) ──────────────────
@app.route('/edit_recipe_header/<int:recipe_id>', methods=['POST'])
def edit_recipe_header_save(recipe_id):
    header_sheet = get_gsheet().open("Recepten").worksheet("RecipeHeader")
    header_data = header_sheet.get_all_records()

    persons = request.form.get('Persons', '')
    if len(persons) > 10:
        flash('Personen mag maximaal 10 tekens zijn.', 'error')
        return redirect(url_for('edit_recipe', recipe_id=recipe_id))

    changes = {f: request.form.get(f, '') for f in ['Remarks', 'Utensils', 'CookingTime', 'Persons']}
    for i, row in enumerate(header_data):
        if row['RecipeId'] == recipe_id:
            _update_row("RecipeHeader", i + 2, changes)
            break
    else:
        next_key = max((int(h['Key']) for h in header_data if str(h.get('Key', '')).strip().isdigit()), default=0) + 1
        new = {'Key': next_key, 'RecipeId': recipe_id, **changes}
        _db().append_rows("RecipeHeader", [new])

    flash('Gegevens opgeslagen.', 'success')
    return redirect(url_for('edit_recipe', recipe_id=recipe_id))


# ── 7.4  POST: Verwijder één Lines-regel ──────────────────────────────────────
# BELANGRIJK: LineId is NIET globaal uniek – altijd matchen op RecipeId + LineId
@app.route('/delete_recipe_line/<int:recipe_id>/<int:line_id>', methods=['POST'])
def delete_recipe_line(recipe_id, line_id):
    try:
        lines_sheet = get_gsheet().open("Recepten").worksheet("Lines")
        lines_data = lines_sheet.get_all_records()

        for i, row in enumerate(lines_data):
            if row.get('LineId') == line_id and row.get('RecipeId') == recipe_id:
                lines_sheet.delete_rows(i + 2)
                print(f"✅ RecipeId {recipe_id} / LineId {line_id} verwijderd (rij {i + 2})")
                return jsonify({'success': True})

        return jsonify({'success': False, 'error': 'Regel niet gevonden'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


# ── 7.5  POST: Sla bestaande en nieuwe ingrediëntregels op ───────────────────
@app.route('/save_recipe_lines/<int:recipe_id>', methods=['POST'])
def save_recipe_lines(recipe_id):
    try:
        data = request.get_json()
        existing_lines = data.get('existing', [])
        new_lines = data.get('new', [])

        lines_sheet = get_gsheet().open("Recepten").worksheet("Lines")
        lines_data = lines_sheet.get_all_records()
        uom_by_id = {str(u.get('UomId', '')).strip(): u.get('UomName', '')
                     for u in get_gsheet().open("Recepten").worksheet("Uom").get_all_records()}

        def fields(src):
            remark = src.get('ArticleRemark', '')
            return {
                'ArticleId': _num(src.get('ArticleId', '')),
                'Qty': _num(src.get('Qty', '')),
                'UomId': _num(src.get('UomId', '')),
                'ArticleRemark': remark,
                'Remark': remark,
                'SinglePlural': single_plural_for(src.get('Qty', ''),
                                                  uom_by_id.get(str(src.get('UomId', '')).strip(), '')),
            }

        # Bestaande regels bijwerken – match op RecipeId + LineId (LineId is NIET globaal uniek!)
        updates = []
        for update in existing_lines:
            line_id = int(update['LineId'])
            for i, row in enumerate(lines_data):
                if row.get('LineId') == line_id and row.get('RecipeId') == recipe_id:
                    updates.append({'row': i + 2, 'values': fields(update)})
                    break
        if updates:
            _db().update_cells("Lines", updates)   # alleen eigen kolommen, formules blijven staan

        # Nieuwe regels toevoegen (met Key; LineId per recept doortellen)
        if new_lines:
            recipe_line_ids = [row.get('LineId', 0) for row in lines_data
                               if str(row.get('RecipeId')) == str(recipe_id) and row.get('LineId')]
            next_line_id = max((int(x) for x in recipe_line_ids if str(x).strip().isdigit()), default=0) + 1
            next_key = max((int(l['Key']) for l in lines_data if str(l.get('Key', '')).strip().isdigit()), default=0) + 1

            rows = []
            for new_line in new_lines:
                values = {'Key': next_key, 'LineId': next_line_id, 'RecipeId': recipe_id, **fields(new_line)}
                rows.append(values)
                next_line_id += 1
                next_key += 1
            _db().append_rows("Lines", rows)

        return jsonify({'success': True})
    except Exception as e:
        print(f"❌ save_recipe_lines fout: {e}")
        return jsonify({'success': False, 'error': str(e)})


def _num(v):
    """'12' → 12, '1.5' → 1.5, '' → ''. Zo komen getallen als getal in de sheet (niet als tekst)."""
    s = str(v).strip().replace(',', '.')
    if not s:
        return ''
    try:
        f = float(s)
        return int(f) if f == int(f) else f
    except ValueError:
        return v


#------------------
# Lokaal draaien: python app.py  (in productie: uvicorn asgi:app, zie Dockerfile)
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=bool(os.environ.get('FLASK_DEBUG')))
