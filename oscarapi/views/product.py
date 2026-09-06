# pylint: disable=unbalanced-tuple-unpacking
from rest_framework import generics
from rest_framework.response import Response
from django.db.models import (
    Case,
    Exists,
    IntegerField,
    OuterRef,
    Prefetch,
    Q,
    Value,
    When,
)

from oscar.core.loading import get_class, get_model

from oscarapi.utils.categories import find_from_full_slug
from oscarapi.utils.loading import get_api_classes, get_api_class
from rest_framework.exceptions import ValidationError
from server.apps.main.queryset_cache import cache_queryset_per_request
from server.apps.vendor.models import Vendor
Store = get_model('stores', 'store')
Selector = get_class("partner.strategy", "Selector")

(
    CategorySerializer,
    ProductLinkSerializer,
    ProductSerializer,
    ProductListSerializer,
    ProductDetailSerializer,
    ProductStockRecordSerializer,
    AvailabilitySerializer,
) = get_api_classes(
    "serializers.product",
    [
        "CategorySerializer",
        "ProductLinkSerializer",
        "ProductSerializer",
        "ProductListSerializer",
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
    """Branch-scoped storefront product list."""

    serializer_class = ProductListSerializer

    def _fuzzy_ids(self, search_query):
        """Elasticsearch candidate ids, computed at most once per request."""
        cached = getattr(self, "_fuzzy_ids_cached", None)
        if cached is not None and cached[0] == search_query:
            return cached[1]

        # Lazy import: server.apps.search imports back into oscarapi.
        from server.apps.search.fuzzy import fuzzy_product_ids

        ids = fuzzy_product_ids(search_query)
        self._fuzzy_ids_cached = (search_query, ids)
        return ids

    # Cached because this get_queryset does real I/O -- an .exists() probe and,
    # on the fallback path, an Elasticsearch query -- and DRF's permission
    # class calls get_queryset() to resolve the model before the view calls it
    # again for the data. Without this both ran twice per request.
    @cache_queryset_per_request
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

        # ProductSerializer walks eleven relations per row. Without these the
        # list issued one query per relation per product (228 for a default
        # page of 20). Mirrors the vendor dashboard list, which already
        # prefetches the same graph -- see
        # server/apps/catalogue/views.py VendorProductListCreateView.
        ProductAttributeMapping = get_model("catalogue", "ProductAttributeMapping")
        qs = (
            Product.objects.filter(
                branches__id=branch_id,
                branches__is_active=True,
                categories__is_public=True,
                is_public=True,
            )
            .distinct()
            # Oscar's own helper: fills `_prefetched_attribute_values`, which
            # is what ProductAttributesContainer checks before falling back to
            # a per-product query.
            .prefetch_attribute_values()
            .select_related("product_class", "parent", "vendor")
            .prefetch_related(
                "images",
                "stockrecords",
                "categories",
                "children",
                "service",
                "allergens",
                # both halves of the Product.options union, read by
                # ProductListSerializer.get_options
                "product_options",
                "product_class__options",
                Prefetch(
                    "simple_attributes",
                    queryset=ProductAttributeMapping.objects.select_related(
                        "attribute", "value"
                    ),
                ),
            )
        )

        structure = self.request.query_params.get("structure")
        if structure:
            qs = qs.filter(structure=structure)

        category_id = self.request.query_params.get("category_id")
        if category_id:
            qs = qs.filter(categories__id=category_id)

        # `?search=` matches the product's own title/description or the name of
        # any category it sits in. Both language columns are matched explicitly
        # rather than relying on `title`/`name`: Product and Category are
        # registered with django-modeltranslation, which rewrites a filter on
        # the base field to the ACTIVE language's column (`title_ar` under
        # /ar/). That rewrite has no fallback -- unlike attribute reads -- so a
        # row whose Arabic column is NULL renders fine under /ar/ but could
        # never be found there, and an English term would miss every product
        # under /ar/ entirely.
        search_query = self.request.query_params.get("search")
        fuzzy_ids = []
        if search_query:
            # Category name is matched via EXISTS rather than a join: the
            # ordering below aggregates with Min("categories__order"), and a
            # second join to `categories` in the WHERE clause would fan rows
            # out and skew that aggregate.
            in_matching_category = Category.objects.filter(
                product=OuterRef("pk")
            ).filter(
                Q(name_en__icontains=search_query)
                | Q(name_ar__icontains=search_query)
            )
            exact = qs.filter(
                Q(title_en__icontains=search_query)
                | Q(title_ar__icontains=search_query)
                | Q(description_en__icontains=search_query)
                | Q(description_ar__icontains=search_query)
                | Q(Exists(in_matching_category))
            )

            # Typo tolerance, as a fallback only. Postgres above is live and
            # exact; Elasticsearch is consulted solely when it found nothing,
            # so a stale or unreachable index costs typo tolerance rather than
            # breaking search. The ids come back as candidates and are
            # re-filtered through `qs`, which still owns every visibility rule
            # (branch membership, active branch, public product, public
            # category) -- ES knows nothing about branches.
            if exact.exists():
                qs = exact
            else:
                fuzzy_ids = self._fuzzy_ids(search_query)
                qs = qs.filter(pk__in=fuzzy_ids) if fuzzy_ids else exact

        # In-stock products first (by category display order, then id);
        # out-of-stock products are pushed to the end of the response.
        from server.apps.catalogue.ordering import apply_branch_stock_ordering

        qs = apply_branch_stock_ordering(qs, branch_id)

        if fuzzy_ids:
            # Keep Elasticsearch's relevance order, but never above the
            # in-stock-first rule the rest of the endpoint guarantees.
            relevance = Case(
                *[
                    When(pk=product_id, then=Value(position))
                    for position, product_id in enumerate(fuzzy_ids)
                ],
                default=Value(len(fuzzy_ids)),
                output_field=IntegerField(),
            )
            qs = qs.annotate(_relevance=relevance).order_by(
                "-_in_stock", "_relevance", "id"
            )

        return qs


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
