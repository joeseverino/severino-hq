from django.http import HttpResponse


def widget_list(request):
    return HttpResponse("widgets")
