from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Q
from django.urls import reverse_lazy
from django.views.generic import (
    CreateView,
    DeleteView,
    DetailView,
    ListView,
    UpdateView,
)

from .forms import ProjectForm
from .models import PROJECT_CATEGORY_CHOICES, Project


class ProjectListView(LoginRequiredMixin, ListView):
    model = Project
    template_name = "projects/project_list.html"
    context_object_name = "projects"
    paginate_by = 25

    def get_queryset(self):
        qs = Project.objects.all()
        q = self.request.GET.get("q", "").strip()
        status = self.request.GET.get("status", "").strip()
        category = self.request.GET.get("category", "").strip()
        sort = self.request.GET.get("sort", "-updated_at")
        if q:
            qs = qs.filter(
                Q(name__icontains=q)
                | Q(description__icontains=q)
                | Q(technologies_used__icontains=q)
                | Q(notes__icontains=q)
            )
        if status:
            qs = qs.filter(status=status)
        if category:
            qs = qs.filter(category=category)
        if sort in {
            "name", "-name", "updated_at", "-updated_at",
            "status", "-status", "category", "-category",
        }:
            qs = qs.order_by(sort)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(
            q=self.request.GET.get("q", ""),
            selected_status=self.request.GET.get("status", ""),
            selected_category=self.request.GET.get("category", ""),
            sort=self.request.GET.get("sort", "-updated_at"),
            status_choices=Project.Status.choices,
            category_choices=PROJECT_CATEGORY_CHOICES,
        )
        return ctx


class ProjectDetailView(LoginRequiredMixin, DetailView):
    model = Project
    template_name = "projects/project_detail.html"
    slug_field = "slug"
    slug_url_kwarg = "slug"
    context_object_name = "project"


class ProjectCreateView(LoginRequiredMixin, CreateView):
    model = Project
    form_class = ProjectForm
    template_name = "projects/project_form.html"

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, f"Project “{self.object}” created.")
        return response


class ProjectUpdateView(LoginRequiredMixin, UpdateView):
    model = Project
    form_class = ProjectForm
    template_name = "projects/project_form.html"
    slug_field = "slug"
    slug_url_kwarg = "slug"

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, f"Project “{self.object}” updated.")
        return response


class ProjectDeleteView(LoginRequiredMixin, DeleteView):
    model = Project
    template_name = "projects/project_confirm_delete.html"
    slug_field = "slug"
    slug_url_kwarg = "slug"
    success_url = reverse_lazy("projects:list")
    context_object_name = "project"

    def form_valid(self, form):
        name = str(self.get_object())
        response = super().form_valid(form)
        messages.success(self.request, f"Project “{name}” deleted.")
        return response
