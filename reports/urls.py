from django.urls import path

from . import views

app_name = "reports"

urlpatterns = [
    path("", views.ReportsView.as_view(), name="dashboard"),
    path("export/expenses.csv", views.ExpensesCSVView.as_view(), name="expenses_csv"),
    path("export/assets.csv", views.AssetsCSVView.as_view(), name="assets_csv"),
    path("export/content.csv", views.ContentCSVView.as_view(), name="content_csv"),
    path("export/projects.csv", views.ProjectsCSVView.as_view(), name="projects_csv"),
    path(
        "export/documentation.csv",
        views.DocumentationCSVView.as_view(),
        name="documentation_csv",
    ),
    path(
        "export/year-summary.json",
        views.YearSummaryJSONView.as_view(),
        name="year_summary_json",
    ),
    path(
        "export/year-summary.md",
        views.YearSummaryMarkdownView.as_view(),
        name="year_summary_md",
    ),
]
