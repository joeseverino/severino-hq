from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Count, Q
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.views.generic import (
    CreateView,
    DeleteView,
    DetailView,
    ListView,
    UpdateView,
)

from application.content import content_command_from_cleaned_data, save_content
from application.deletion import DeleteCommand, delete_content
from application.security import web_principal
from .forms import ContentItemForm
from .models import ContentItem


class ContentListView(LoginRequiredMixin, ListView):
    model = ContentItem
    template_name = "content/content_list.html"
    context_object_name = "items"
    paginate_by = 25

    def get_queryset(self):
        qs = ContentItem.objects.all()
        q = self.request.GET.get("q", "").strip()
        status = self.request.GET.get("status", "").strip()
        ctype = self.request.GET.get("content_type", "").strip()
        sort = self.request.GET.get("sort", "-updated_at")
        if q:
            qs = qs.filter(
                Q(title__icontains=q)
                | Q(topic__icontains=q)
                | Q(tags__icontains=q)
                | Q(notes__icontains=q)
            )
        if status:
            qs = qs.filter(status=status)
        if ctype:
            qs = qs.filter(content_type=ctype)
        if sort in {
            "title", "-title", "updated_at", "-updated_at",
            "status", "-status", "content_type", "-content_type",
            "published_at", "-published_at",
        }:
            qs = qs.order_by(sort)
        if self.request.GET.get("no_docs"):
            qs = qs.annotate(doc_count=Count("related_documentation")).filter(
                doc_count=0
            )
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(
            q=self.request.GET.get("q", ""),
            selected_status=self.request.GET.get("status", ""),
            selected_type=self.request.GET.get("content_type", ""),
            sort=self.request.GET.get("sort", "-updated_at"),
            status_choices=ContentItem.Status.choices,
            type_choices=ContentItem.Type.choices,
            no_docs=self.request.GET.get("no_docs", ""),
        )
        return ctx


class ContentDetailView(LoginRequiredMixin, DetailView):
    model = ContentItem
    template_name = "content/content_detail.html"
    slug_field = "slug"
    slug_url_kwarg = "slug"
    context_object_name = "item"
    queryset = ContentItem.objects.prefetch_related(
        "related_projects",
        "related_assets",
        "related_documentation",
        "related_expenses",
    )


class ContentCreateView(LoginRequiredMixin, CreateView):
    model = ContentItem
    form_class = ContentItemForm
    template_name = "content/content_form.html"

    def form_valid(self, form):
        result = save_content(
            content_command_from_cleaned_data(form.cleaned_data),
            principal=web_principal(self.request.user),
        )
        self.object = ContentItem.objects.get(slug=result["content"]["slug"])
        messages.success(self.request, f"Content item “{self.object}” created.")
        return redirect(self.object.get_absolute_url())


class ContentUpdateView(LoginRequiredMixin, UpdateView):
    model = ContentItem
    form_class = ContentItemForm
    template_name = "content/content_form.html"
    slug_field = "slug"
    slug_url_kwarg = "slug"

    def form_valid(self, form):
        result = save_content(
            content_command_from_cleaned_data(form.cleaned_data),
            principal=web_principal(self.request.user),
            current_slug=self.get_object().slug,
        )
        self.object = ContentItem.objects.get(slug=result["content"]["slug"])
        messages.success(self.request, f"Content item “{self.object}” updated.")
        return redirect(self.object.get_absolute_url())


class ContentDeleteView(LoginRequiredMixin, DeleteView):
    model = ContentItem
    template_name = "content/content_confirm_delete.html"
    slug_field = "slug"
    slug_url_kwarg = "slug"
    success_url = reverse_lazy("content:list")
    context_object_name = "item"

    def form_valid(self, form):
        slug = self.get_object().slug
        result = delete_content(
            DeleteCommand(confirm=slug),
            principal=web_principal(self.request.user),
            current_slug=slug,
        )
        messages.success(self.request, f"Content item “{result['deleted']['label']}” deleted.")
        return redirect(self.success_url)
