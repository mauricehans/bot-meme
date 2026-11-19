from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("tracking/<int:tracking_id>/", views.tracking_detail, name="tracking_detail"),
    path("tracking/<int:tracking_id>/address/<str:address>/", views.address_detail, name="address_detail"),
    path("tracking/<int:tracking_id>/start/", views.start_tracking, name="start_tracking"),
    path("tracking/<int:tracking_id>/stop/", views.stop_tracking, name="stop_tracking"),
    path("tracking/<int:tracking_id>/add-address/", views.add_address, name="add_address"),
    path("tracking/<int:tracking_id>/graph/", views.tracking_graph, name="tracking_graph"),
]