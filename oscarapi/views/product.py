# pylint: disable=unbalanced-tuple-unpacking
from rest_framework import generics
from rest_framework.response import Response
from django.db.models import Q

from oscar.core.loading import get_class, get_model

from oscarapi.utils.categories import find_from_full_slug
from oscarapi.utils.loading import get_api_classes, get_api_class
from rest_framework.exceptions import ValidationError
from server.apps.vendor.models import Vendor
Store = get_model('stores', 'store')
Selector = get_class("partner.strategy", "Selector")

(
    CategorySerializer,
    ProductLinkSerializer,
    ProductSerializer,
    ProductDetailSerializer,
    ProductStockRecordSerializer,
    AvailabilitySerializer,
) = get_api_classes(
    "serializers.product",
    [
        "CategorySerializer",
        "ProductLinkSerializer",
        "ProductSerializer",
        "ProductDetailSerializer",
        "ProductStockRecordSerializer",
        "AvailabilitySerializer",
    ],
)

PriceSerializer = get_api_class("serializers.checkout", "PriceSerializer")


__all__ = ("ProductList", "ProductDetail", "ProductPrice", "ProductAvailability")

Product = get_model("catalogue", "Product")
Category = get_model("catalogue", "Category")
StockRecord = get_model("partner", "StockRecord")


class ProductList(generics.ListAPIView):
    serializer_class = ProductSerializer

    def get_queryset(self):
        """
        Filters products based on:
        - branch_id (required)
        - at least one category is_public
        - branch is_active
        - optional structure
        """
        branch_id = self.request.query_params.get("branch_id")
        if not branch_id:
            raise ValidationError({"branch_id": "This parameter is required."})

        qs = Product.objects.filter(
            branches__id=branch_id,
            branches__is_active=True,
            categories__is_public=True,
            is_public=True,
        ).distinct()

        structure = self.request.query_params.get("structure")
        if structure:
            qs = qs.filter(structure=structure)

        category_id = self.request.query_params.get("category_id")
        if category_id:
            qs = qs.filter(categories__id=category_id)

        # In-stock products first (by category display order, then id);
        # out-of-stock products are pushed to the end of the response.
        from server.apps.catalogue.ordering import apply_branch_stock_ordering

        return apply_branch_stock_ordering(qs, branch_id)


class ProductDetail(generics.RetrieveAPIView):
    """Single product, with the extra ``recommended`` section the storefront
    shows underneath it. Pass ``?branch_id=`` so both the stock record and the
    recommendations are scoped to the branch being browsed."""

    queryset = Product.objects.all()
    serializer_class = ProductDetailSerializer


class ProductPrice(generics.RetrieveAPIView):
    queryset = Product.objects.all()
    serializer_class = PriceSerializer

    def get(
        self, request, *args, **kwargs
    ):  # pylint: disable=redefined-builtin,arguments-differ
        product = self.get_object()
        strategy = Selector().strategy(request=request, user=request.user)
        ser = PriceSerializer(
            strategy.fetch_for_product(product).price, context={"request": request}
        )
        return Response(ser.data)


class ProductStockRecords(generics.ListAPIView):
    serializer_class = ProductStockRecordSerializer
    queryset = StockRecord.objects.all()

    def get_queryset(self):
        product_pk = self.kwargs.get("pk")
        return super().get_queryset().filter(product_id=product_pk)


class ProductStockRecordDetail(generics.RetrieveAPIView):
    serializer_class = ProductStockRecordSerializer
    queryset = StockRecord.objects.all()


class ProductAvailability(generics.RetrieveAPIView):
    queryset = Product.objects.all()
    serializer_class = AvailabilitySerializer

    def get(
        self, request, *args, **kwargs
    ):  # pylint: disable=redefined-builtin,arguments-differ
        product = self.get_object()
        strategy = Selector().strategy(request=request, user=request.user)
        ser = AvailabilitySerializer(
            strategy.fetch_for_product(product).availability,
            context={"request": request},
        )
        return Response(ser.data)


class CategoryList(generics.ListAPIView):
    serializer_class = CategorySerializer

    def get_queryset(self):
        """
        Fetches the root nodes or children of a category filtered by vendor if provided.
        """
        breadcrumb_path = self.kwargs.get("breadcrumbs", None)
        branch_id = self.request.query_params.get("branch", None)
        search_query = self.request.query_params.get("search", None)

        # Ensure branch_id is provided
        if not branch_id:
            raise ValidationError("branch parameter is required.")

        # Get the store and its vendor
        try:
            store = Store.objects.get(id=branch_id)
            vendor = store.vendor  # Access the related vendor
            
            # Validate store and vendor are active
            if not store.is_active:
                raise ValidationError({"branch": "This store is not active."})
            if not vendor.is_valid:
                raise ValidationError({"branch": "This vendor is not active."})
                
        except Store.DoesNotExist:
            raise ValidationError({"branch": f"No store found with ID {branch_id}."})
        except AttributeError:
            raise ValidationError({"branch": "This store has no associated vendor."})

        # Return this level of the tree for the branch's vendor. Descendants are
        # nested recursively by CategorySerializer, so we only return the top of
        # the requested subtree here (root nodes, or the breadcrumb's children).
        # All categories are shown -- no pruning to only those with in-stock
        # products -- and each category embeds all of its products for this
        # branch (CategorySerializer.get_products).
        queryset = (
            find_from_full_slug(breadcrumb_path, "/").get_children()
            if breadcrumb_path else
            Category.get_root_nodes()
        ).filter(vendor=vendor).distinct()

        # Search filters on the category itself (name/description), not products.
        if search_query:
            queryset = queryset.filter(
                Q(name__icontains=search_query) |
                Q(description__icontains=search_query)
            )

        return queryset.order_by("order", "id")


class CategoryDetail(generics.RetrieveAPIView):
    queryset = Category.objects.all()
    serializer_class = CategorySerializer
