"""Help Centre Blueprint — TSS Reference Documentation Hub."""
import os
from flask import Blueprint, render_template, send_from_directory, abort

help_bp = Blueprint('help', __name__,
    template_folder='../../templates/help',
    url_prefix='/help')


def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _help_docs_dirs():
    repo_root = _repo_root()
    return [
        os.path.join(repo_root, 'docs', 'help'),
        os.path.join(repo_root, 'app', 'static', 'help'),
    ]

# Document registry — maps slugs to files + metadata
HELP_DOCS = [
    {
        'slug': 'api-reference',
        'file': 'TSS-API-Reference.html',
        'title': 'TSS API Interactive Reference',
        'desc': 'Full interactive API explorer with payload builder, schema viewer, and SQL generator for all 12 TSS resources.',
        'icon': 'bi-code-slash',
        'color': '#1a5ccc',
        'category': 'API',
    },
    {
        'slug': 'api-spec',
        'file': 'TSS-API-Reference-v2_9_4.html',
        'title': 'TSS API Reference v2.9.4',
        'desc': 'Complete field-level specification for all TSS Declaration API endpoints including mandatory/conditional rules.',
        'icon': 'bi-file-earmark-code',
        'color': '#0b1d3a',
        'category': 'API',
    },
    {
        'slug': 'process-cheatsheet',
        'file': 'TSS_Declaration_Process_CheatSheet__2_.html',
        'title': 'Declaration Process Cheat Sheet',
        'desc': 'Step-by-step visual guide covering the full declaration lifecycle from ENS through to SDI completion.',
        'icon': 'bi-map',
        'color': '#10b981',
        'category': 'Process',
    },
    {
        'slug': 'route-a-actors-data',
        'file': 'Route_A_Actors_and_Data.html',
        'title': 'Route A Actors And Data',
        'desc': 'Route A reference map showing operational actors, master data ownership and transactional data flow.',
        'icon': 'bi-truck',
        'color': '#0891b2',
        'category': 'Process',
    },
    {
        'slug': 'declaration-types',
        'file': 'TSS_Declaration_Types_DeepDive.html',
        'title': 'Declaration Types Deep Dive',
        'desc': 'Detailed breakdown of all declaration types — ENS, SFD, Supplementary, FFD, IMMI — when to use each and how they relate.',
        'icon': 'bi-diagram-3',
        'color': '#7c3aed',
        'category': 'Reference',
    },
    {
        'slug': 'ens-goods-supdec-spec',
        'file': 'TSS_Spec_ENS_Goods_SupDec__1_.html',
        'title': 'ENS + Goods + SupDec Specification',
        'desc': 'Field-by-field specification for ENS Headers, Consignments, Goods Items, and Supplementary Declarations with validation rules.',
        'icon': 'bi-list-check',
        'color': '#d97706',
        'category': 'Specification',
    },
    {
        'slug': 'tss-actors-master-data',
        'file': 'TSS_Actors_MasterData.html',
        'title': 'TSS Actors And Master Data',
        'desc': 'Classification guide for TSS actors, reusable master data and movement-specific transactional fields.',
        'icon': 'bi-people',
        'color': '#c41e1e',
        'category': 'Reference',
    },
    {
        'slug': 'tss-data-model-synovia',
        'file': 'TSS_DataModel_Synovia.html',
        'title': 'TSS Declaration Data Model',
        'desc': 'Interactive declaration data model showing field relationships, validation dependencies and TSS object links.',
        'icon': 'bi-diagram-2',
        'color': '#4a90d9',
        'category': 'Reference',
    },
]


@help_bp.route('/')
def index():
    """Help Centre landing page with document cards."""
    categories = {}
    for doc in HELP_DOCS:
        cat = doc['category']
        if cat not in categories:
            categories[cat] = []
        categories[cat].append(doc)
    return render_template('help/index.html', docs=HELP_DOCS, categories=categories)


@help_bp.route('/doc/<slug>')
def view_doc(slug):
    """Serve a help document in an iframe within the portal chrome."""
    doc = next((d for d in HELP_DOCS if d['slug'] == slug), None)
    if not doc:
        abort(404)
    return render_template('help/viewer.html', doc=doc)


@help_bp.route('/raw/<slug>')
def raw_doc(slug):
    """Serve the raw HTML file (for iframe src)."""
    doc = next((d for d in HELP_DOCS if d['slug'] == slug), None)
    if not doc:
        abort(404)
    for docs_dir in _help_docs_dirs():
        if os.path.isfile(os.path.join(docs_dir, doc['file'])):
            return send_from_directory(docs_dir, doc['file'])
    abort(404)
