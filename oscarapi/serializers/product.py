# pylint: disable=W0632

import logging
from copy import deepcopy
from typing import Any
from django.db.models.manager import Manager
from django.utils.translation import gettext as _

from rest_framework import serializers
from rest_framework.fields import empty
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field, inline_serializer

from oscar.core.loading import get_model
from oscarapi.basket import operations
from django.db.models import F

from oscarapi.utils.exists import bound_unique_together_get_or_create_multiple
from oscarapi.utils.loading import get_api_classes
from oscarapi import settings
from oscarapi.utils.files import file_hash
from oscarapi.utils.exists import find_existing_attribute_option_group
from oscarapi.utils.accessors import getitems
from oscarapi.serializers.fields import DrillDownHyperlinkedIdentityField
from oscarapi.utils.attributes import AttributeConverter
from oscarapi.serializers.utils import (
    OscarModelSerializer,
    OscarHyperlinkedModelSerializer,
    UpdateListSerializer,
    UpdateForwardManyToManySerializer,
)
from server.apps.service.models import Service
from server.apps.vendor.models import Vendor

from .exceptions import FieldError

logger = logging.getLogger(__name__)
Product = get_model("catalogue", "Product")
Range = get_model("offer", "Range")
ProductAttributeValue = get_model("catalogue", "ProductAttributeValue")
ProductImage = get_model("catalogue", "ProductImage")
Option = get_model("catalogue", "Option")
Partner = get_model("partner", "Partner")
StockRecord = get_model("partner", "StockRecord")
ProductClass = get_model("catalogue", "ProductClass")
ProductAttribute = get_model("catalogue", "ProductAttribute")
Category = get_model("catalogue", "Category")
AttributeOption = get_model("catalogue", "AttributeOption")
AttributeOptionGroup = get_model("catalogue", "AttributeOptionGroup")
AttributeValueField, CategoryField, SingleValueSlugRelatedField = get_api_classes(
    "serializers.fields",
    ["AttributeValueField", "CategoryField", "SingleValueSlugRelatedField"],
)





class ServiceSerializer(OscarModelSerializer):
    """
    Serializer for the Service model.
    """
    
    available_time_slots = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = Service
        fields = [
            "id",
            "product",
            "branch",
            "service_type",
            "provider_name",
            "duration_minutes",
            "max_services_per_slot",
            "max_notice_days",
            "location_type",
            "available_time_slots",
        ]
        # If you want a custom list serializer that supports "updates", 
        # you can set:
        # list_serializer_class = SomeUpdateListSerializer
        
    @extend_schema_field(
        inline_serializer(
            name="ServiceAvailableDaySerializer",
            fields={
                "date": serializers.CharField(),
                "weekday": serializers.CharField(),
                "slots": inline_serializer(
                    name="ServiceAvailableSlotSerializer",
                    fields={
                        "start": serializers.CharField(),
                        "end": serializers.CharField(),
                    },
                    many=True,
                ),
            },
            many=True,
        )
    )
    def get_available_time_slots(self, obj) -> list[dict[str, Any]]:
        """
        Call the Service model method that calculates available slots.
        """
        return obj.get_available_time_slots()
    
class AttributeOptionSerializer(serializers.ModelSerializer):
    """
    Serializer for AttributeOption to include the price field.
    """
    class Meta:
        model = AttributeOption
        fields = ['id', 'option', 'price']  # Include 'price' here


class AttributeOptionGroupSerializer(OscarHyperlinkedModelSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name="attributeoptiongroup-detail"
    )
    options = AttributeOptionSerializer(many=True, required=True)

    def create(self, validated_data):
        options_data = validated_data.pop('options', [])
        instance = super().create(validated_data)
        options = [AttributeOption.objects.get_or_create(**option_data)[0] for option_data in options_data]
        instance.options.set(options)
        return instance

    def update(self, instance, validated_data):
        options_data = validated_data.pop('options', [])
        instance = super().update(instance, validated_data)
        options = [AttributeOption.objects.get_or_create(**option_data)[0] for option_data in options_data]
        instance.options.set(options)
        return instance

    class Meta:
        model = AttributeOptionGroup
        fields = ("id", "url", "name", "code", "options")
        
        depth = 1



class BaseCategorySerializer(OscarHyperlinkedModelSerializer):
    breadcrumbs = serializers.CharField(source="full_name", read_only=True)
    vendor = serializers.HyperlinkedRelatedField(
        view_name="vendor-detail",
        queryset=Vendor.objects.all(),
        lookup_field="pk",
    )
    id = serializers.IntegerField(read_only=True)

    class Meta:
        model = Category
        exclude = ("path", "depth", "numchild")


class CategorySerializer(BaseCategorySerializer):
    """`api/categories/?branch=` returns each category's full detail with its
    descendants nested recursively (the same detail shape, not a link) and all
    of the category's products embedded under ``products``.
    """

    children = serializers.SerializerMethodField()
    products = serializers.SerializerMethodField()

    def get_children(self, obj):
        """
        Recursively serialize this category's direct children with the same
        serializer, so the response is the full subtree of category details
        rather than a link to a child-list endpoint.
        """
        children = obj.get_children().filter(vendor_id=obj.vendor_id).order_by(
            "order", "id"
        )
        return CategorySerializer(children, many=True, context=self.context).data

    def get_products(self, obj):
        """
        Every public product directly in this category, with no paging or
        pruning. When the request is branch-scoped (``?branch=``, which
        CategoryList requires) only products sold at that active branch are
        returned, in-stock first -- the same contract as ProductList. Products
        of subcategories appear under their own entry in ``children``.

        Scoped to the category's own vendor. Product.categories is a plain
        many-to-many with nothing stopping a row from pointing at another
        vendor's category, and CategoryList only filters the *categories* by
        the branch's vendor -- so without this a foreign vendor's product
        renders inside this vendor's tree. `get_children` scopes the subtree
        by vendor for the same reason.
        """
        from server.apps.catalogue.ordering import apply_branch_stock_ordering

        products = obj.product_set.filter(is_public=True, vendor_id=obj.vendor_id)
        request = self.context.get("request")
        branch_id = request.query_params.get("branch") if request else None
        if branch_id:
            products = apply_branch_stock_ordering(
                products.filter(
                    branches__id=branch_id, branches__is_active=True
                ).distinct(),
                branch_id,
            )
        else:
            products = products.distinct().order_by("id")
        return ProductSerializer(products, many=True, context=self.context).data


    # class Meta(BaseCategorySerializer.Meta):
    #     fields = BaseCategorySerializer.Meta.fields + ['children', 'products']



class ProductAttributeListSerializer(UpdateListSerializer):
    def select_existing_item(self, manager, datum):
        try:
            return manager.get(product_class=datum["product_class"], code=datum["code"])
        except manager.model.DoesNotExist:
            pass
        except manager.model.MultipleObjectsReturned as e:
            logger.error("Multiple objects on unique contrained items, freaky %s", e)
            logger.exception(e)

        return None


class ProductAttributeSerializer(OscarHyperlinkedModelSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name="admin-productattribute-detail"
    )
    product_class = serializers.SlugRelatedField(
        slug_field="slug",
        queryset=ProductClass.objects.get_queryset(),
        write_only=True,
        required=False,
    )
    option_group = AttributeOptionGroupSerializer(required=False, allow_null=True)

    def create(self, validated_data):
        option_group = validated_data.pop("option_group", None)
        instance = super(ProductAttributeSerializer, self).create(validated_data)
        return self.update(instance, {"option_group": option_group})

    def update(self, instance, validated_data):
        option_group = validated_data.pop("option_group", None)
        updated_instance = super(ProductAttributeSerializer, self).update(
            instance, validated_data
        )
        if option_group is not None:
            serializer = self.fields["option_group"]
            # use the serializer to update the attribute_values
            if instance.option_group:
                updated_instance.option_group = serializer.update(
                    instance.option_group, option_group
                )
            else:
                updated_instance.option_group = serializer.create(option_group)

            updated_instance.save()

        return updated_instance

    class Meta:
        model = ProductAttribute
        list_serializer_class = ProductAttributeListSerializer
        fields = "__all__"


class RangeSerializer(OscarHyperlinkedModelSerializer):
    class Meta:
        model = Range
        fields = "__all__"


class PartnerSerializer(OscarHyperlinkedModelSerializer):
    class Meta:
        model = Partner
        fields = "__all__"


class OptionSerializer(OscarHyperlinkedModelSerializer):
    code = serializers.SlugField()
    option_group = AttributeOptionGroupSerializer(required=True)
    class Meta:
        model = Option
        fields = settings.OPTION_FIELDS
        list_serializer_class = UpdateForwardManyToManySerializer
        depth = 1

class ProductAttributeValueListSerializer(UpdateListSerializer):
    # pylint: disable=unused-argument
    def shortcut_to_internal_value(self, data, productclass, attributes):
        difficult_attributes = {
            at.code: at
            for at in productclass.attributes.filter(
                type__in=[
                    ProductAttribute.OPTION,
                    ProductAttribute.MULTI_OPTION,
                    ProductAttribute.DATE,
                    ProductAttribute.DATETIME,
                    ProductAttribute.ENTITY,
                    ProductAttribute.FILE,
                    ProductAttribute.IMAGE,
                ]
            )
        }
        cv = AttributeConverter(self.context)
        internal_value = []
        for item in data:
            code, value = getitems(item, "code", "value")
            if code is None:  # delegate error state to child serializer
                internal_value.append(self.child.to_internal_value(item))

            if code in difficult_attributes:
                attribute = difficult_attributes[code]
                converted_value = cv.to_attribute_type_value(attribute, code, value)
                internal_value.append(
                    {
                        "value": converted_value,
                        "attribute": attribute,
                        "product_class": productclass,
                    }
                )
            else:
                internal_value.append(
                    {
                        "value": value,
                        "attribute": code,
                        "product_class": productclass,
                    }
                )

        return internal_value

    def to_internal_value(self, data):
        productclasses = set()
        attributes = set()
        parent = None

        for item in data:
            product_class, code = getitems(item, "product_class", "code")
            if product_class:
                productclasses.add(product_class)
            if "parent" in item and item["parent"] is not None:
                parent = item["parent"]
            attributes.add(code)

        # if all attributes belong to the same productclass, everything is just
        # as expected and we can take a shortcut by only resolving the
        # productclass to the model instance and nothing else.
        attrs_valid = all(attributes)  # no missing attribute codes?
        if attrs_valid:
            try:
                if len(productclasses):
                    (product_class,) = productclasses
                    pc = ProductClass.objects.get(slug=product_class)
                    return self.shortcut_to_internal_value(data, pc, attributes)
                elif parent:
                    pc = ProductClass.objects.get(products__id=parent)
                    return self.shortcut_to_internal_value(data, pc, attributes)
            except ProductClass.DoesNotExist:
                pass

        # if we get here we can't take the shortcut, just let everything be
        # processed by the original serializer and handle the errors.
        return super().to_internal_value(data)

    def get_value(self, dictionary):
        values = super(ProductAttributeValueListSerializer, self).get_value(dictionary)
        if values is empty:
            return values

        product_class, parent = getitems(dictionary, "product_class", "parent")
        return [
            dict(value, product_class=product_class, parent=parent) for value in values
        ]

    def to_representation(self, data):
        if isinstance(data, Manager):
            # use a cached query from product.attr to get the attributes instead
            # if an silly .all() that clones the queryset and performs a new query
            _, product = self.get_name_and_rel_instance(data)
            iterable = product.attr.get_values()
        else:
            iterable = data

        return [self.child.to_representation(item) for item in iterable]

    def update(self, instance, validated_data):
        assert isinstance(instance, Manager)

        _, product = self.get_name_and_rel_instance(instance)

        attr_codes = []
        product.attr.initialize()
        for validated_datum in validated_data:
            # leave all the attribute saving to the ProductAttributesContainer instead
            # of the child serializers
            attribute, value = getitems(validated_datum, "attribute", "value")
            if hasattr(
                attribute, "code"
            ):  # if the attribute is a model instance use the code
                product.attr.set(attribute.code, value, validate_identifier=False)
                attr_codes.append(attribute.code)
            else:
                product.attr.set(attribute, value, validate_identifier=False)
                attr_codes.append(attribute)

        # if we don't clear the dirty attributes all parent attributes
        # are marked as explicitly set, so they will be copied to the
        # child product.
        product.attr._dirty.clear()  # pylint: disable=protected-access
        product.attr.save()
        # we have to make sure to use the correct db_manager in a multidatabase
        # context, we make sure to use the same database as the passed in manager
        local_attribute_values = product.attribute_values.db_manager(
            instance.db
        ).filter(attribute__code__in=attr_codes)
        return list(local_attribute_values)


class ProductAttributeValueSerializer(OscarModelSerializer):
    # we declare the product as write_only since this serializer is meant to be
    # used nested inside a product serializer.
    product = serializers.PrimaryKeyRelatedField(
        many=False, write_only=True, required=False, queryset=Product.objects
    )

    value = AttributeValueField()  # handles different attribute value types
    # while code is specified as read_only, it is still required, because otherwise
    # the attribute is unknown, so while it will never be overwritten, you do
    # have to include it in your data structure
    code = serializers.CharField(source="attribute.code", read_only=True)
    name = serializers.CharField(
        source="attribute.name", required=False, read_only=True
    )

    def to_internal_value(self, data):
        try:
            internal_value = super(
                ProductAttributeValueSerializer, self
            ).to_internal_value(data)
            internal_value["product_class"] = data.get("product_class")
            return internal_value
        except FieldError as e:
            raise serializers.ValidationError(e.detail)

    def save(self, **kwargs):
        """
        Since there is a unique constraint, sometimes we want to update instead
        of creating a new object (because an integrity error would occur due
        to the constraint on attribute and product). If instance is set, the
        update method will be used instead of the create method.
        """
        data = deepcopy(kwargs)
        data.update(self.validated_data)
        return self.update_or_create(data)

    def update_or_create(self, validated_data):
        value = validated_data["value"]
        product = validated_data["product"]
        attribute = validated_data["attribute"]
        attribute.save_value(product, value)
        return product.attribute_values.get(attribute=attribute)

    create = update_or_create

    def update(self, instance, validated_data):
        data = deepcopy(validated_data)
        data["product"] = instance.product
        return self.update_or_create(data)

    class Meta:
        model = ProductAttributeValue
        list_serializer_class = ProductAttributeValueListSerializer
        fields = settings.PRODUCT_ATTRIBUTE_VALUE_FIELDS


class ProductImageUpdateListSerializer(UpdateListSerializer):
    "Select existing image based on hash of image content"

    def select_existing_item(self, manager, datum):
        # determine the hash of the passed image
        target_file_hash = file_hash(datum["original"])
        for image in manager.all():  # search for a match in the set of exising images
            _hash = file_hash(image.original)
            if _hash == target_file_hash:
                # django will create a copy of the original under a weird name,
                # because the image is freshly fetched, except if we use the
                # original image FileObject
                datum["original"] = image.original
                return image

        return None


class ProductImageSerializer(OscarModelSerializer):
    product = serializers.PrimaryKeyRelatedField(
        write_only=True, required=False, queryset=Product.objects
    )
    original = serializers.ImageField(required=False)
    
    def create(self, validated_data):
        """
        Handle image upload when creating a new product image.
        """
        return super().create(validated_data)
    
    def update(self, instance, validated_data):
        """
        Handle image upload when updating an existing product image.
        """
        return super().update(instance, validated_data)

    class Meta:
        model = ProductImage
        fields = "__all__"
        list_serializer_class = ProductImageUpdateListSerializer


class AvailabilitySerializer(serializers.Serializer):  # pylint: disable=abstract-method
    is_available_to_buy = serializers.BooleanField()
    num_available = serializers.IntegerField(required=False)
    message = serializers.CharField()


class RecommmendedProductSerializer(OscarModelSerializer):
    url = serializers.HyperlinkedIdentityField(view_name="product-detail")

    class Meta:
        model = Product
        fields = settings.RECOMMENDED_PRODUCT_FIELDS


class ProductStockRecordSerializer(OscarModelSerializer):
    available_to_buy = serializers.SerializerMethodField()
    in_stock = serializers.SerializerMethodField()
    
    @extend_schema_field(OpenApiTypes.INT)
    def get_available_to_buy(self, obj) -> int:
        """
        Calculate the available to buy quantity as num_in_stock - num_allocated.
        """
        try:
            # Handle the case where num_in_stock or num_allocated might be None
            num_in_stock = obj.num_in_stock or 0
            num_allocated = obj.num_allocated or 0
            return max(0, num_in_stock - num_allocated)
        except Exception:
            # Return 0 as a safe default if any error occurs
            return 0
    @extend_schema_field(OpenApiTypes.BOOL)
    def get_in_stock(self, obj) -> bool:
        """
        Check if the product is in stock.
        """
        try:
            # Handle the case where num_in_stock or num_allocated might be None
            num_in_stock = obj.num_in_stock or 0
            num_allocated = obj.num_allocated or 0
            return max(0, num_in_stock - num_allocated) > 0
        except Exception:
            # Return False as a safe default if any error occurs
            return False
    
    class Meta:
        model = StockRecord
        fields = "__all__"


class BaseProductSerializer(OscarModelSerializer):
    "Base class shared by admin and public serializer"
    attributes = ProductAttributeValueSerializer(
        many=True, required=False, source="attribute_values"
    )
    # categories = CategorySerializer(many=True, required=False)
    product_class = serializers.SlugRelatedField(
        slug_field="slug", queryset=ProductClass.objects, allow_null=True
    )
    options = OptionSerializer(many=True, required=False)
    recommended_products = serializers.HyperlinkedRelatedField(
        view_name="product-detail",
        many=True,
        required=False,
        queryset=Product.objects.filter(
            structure__in=[Product.PARENT, Product.STANDALONE]
        ),
    )

    def validate(self, attrs):
        if "structure" in attrs and "parent" in attrs:
            if attrs["structure"] == Product.CHILD and attrs["parent"] is None:
                raise serializers.ValidationError(_("child without parent"))
        if "structure" in attrs and "product_class" in attrs:
            if attrs["product_class"] is None and attrs["structure"] != Product.CHILD:
                raise serializers.ValidationError(
                    _("product_class can not be empty for structure %(structure)s")
                    % attrs
                )

        return super(BaseProductSerializer, self).validate(attrs)

    class Meta:
        model = Product


class PublicProductSerializer(BaseProductSerializer):
    "Serializer base class used for public products api"
    url = serializers.HyperlinkedIdentityField(view_name="product-detail")
    # price = serializers.HyperlinkedIdentityField(
    #     view_name="product-price", read_only=True
    # )
    availability = serializers.HyperlinkedIdentityField(
        view_name="product-availability", read_only=True
    )

    def get_field_names(self, declared_fields, info):
        """
        Override get_field_names to make sure that we are not getting errors
        for not including declared fields.
        """
        return super(PublicProductSerializer, self).get_field_names({}, info)


class ChildProductSerializer(PublicProductSerializer):
    "Serializer for child products"
    parent = serializers.HyperlinkedRelatedField(
        view_name="product-detail",
        queryset=Product.objects.filter(structure=Product.PARENT),
    )
    # the below fields can be filled from the parent product if enabled.
    images = ProductImageSerializer(many=True, required=False, source="parent.images")
    description = serializers.CharField(source="parent.description")
    stockrecords = ProductStockRecordSerializer(many=True, required=False)

    class Meta(PublicProductSerializer.Meta):
        fields = settings.CHILDPRODUCTDETAIL_FIELDS

class VendorSerializer(serializers.ModelSerializer):

    class Meta:
        model = Vendor
        fields = ['id', 'name']
        
def resolve_branch_id(request):
    """The branch a storefront request is scoped to, or ``None``.

    Tried in order: an explicit ``branch_id``/``branch`` query parameter (the
    storefront passes one of these -- ``ProductList`` uses ``branch_id``,
    ``CategoryList`` uses ``branch``), then the requester's basket, then the
    branch of a logged-in vendor staff member. A vendor's auto-created
    super_admin staff row has ``branch=None``, so that step is guarded.
    """
    if request is None:
        return None

    branch_id = request.query_params.get("branch_id") or request.query_params.get(
        "branch"
    )
    if branch_id:
        return branch_id

    basket_branch = getattr(operations.get_basket(request), "branch", None)
    if basket_branch is not None:
        return basket_branch.pk

    staff = getattr(request.user, "user_vendor_staff", None)
    staff_branch = getattr(staff, "branch", None) if staff is not None else None
    return staff_branch.pk if staff_branch is not None else None


class ProductSerializer(PublicProductSerializer):
    "Serializer for public api with strategy fields added for price and availability"
    # url = serializers.HyperlinkedIdentityField(view_name="product-detail")
    # price = serializers.HyperlinkedIdentityField(
    #     view_name="product-price", read_only=True
    # )
    vendor = VendorSerializer(read_only=True)
    availability = serializers.HyperlinkedIdentityField(
        view_name="product-availability", read_only=True
    )

    images = ProductImageSerializer(many=True, required=False)
    children = ChildProductSerializer(many=True, required=False)
    # stockrecords = serializers.HyperlinkedIdentityField(
    #     view_name="product-stockrecords", read_only=True
    # )

    services = ServiceSerializer(
        many=True,
        required=False,
        source="service",  # or 'services' if using a ManyToMany
    )
    stockrecords = serializers.SerializerMethodField()
    allergens = serializers.SerializerMethodField()
    
    @extend_schema_field(OpenApiTypes.OBJECT)
    def get_allergens(self, obj) -> list[dict[str, Any]]:
        # Lazy import to avoid circular dependency
        from server.apps.catalogue.serializers import AllergenSerializer
        return AllergenSerializer(obj.allergens.all(), many=True).data
        
    @extend_schema_field(ProductStockRecordSerializer)
    def get_stockrecords(self, obj) -> dict[str, Any]:
        """
        Retrieve the stock record for the product at the request's branch.
        """
        branch_id = resolve_branch_id(self.context["request"])
        try:
            wanted = int(branch_id) if branch_id is not None else None
        except (TypeError, ValueError):
            return {}

        # Match in Python against the related manager rather than calling
        # .get(): a .get() always issues its own query, so it cost one round
        # trip per serialized product even when the caller prefetched
        # `stockrecords`. Same approach as the vendor dashboard serializer in
        # server/apps/catalogue/serializers.py.
        stockrecord = next(
            (s for s in obj.stockrecords.all() if s.branch_id == wanted), None
        )
        if stockrecord is None:
            return {}
        return ProductStockRecordSerializer(stockrecord).data

    class Meta(PublicProductSerializer.Meta):
        fields = settings.PRODUCTDETAIL_FIELDS


class ProductListSerializer(ProductSerializer):
    """``ProductSerializer`` with ``options`` resolved from prefetched data.

    ``Product.options`` is a property that ORs two querysets together
    (``product_class.options`` | ``product_options``). A property cannot be
    prefetched, so it issued its own query for every row of a listing -- the
    last per-row query left on ``api/products/``.

    Deliberately a separate subclass rather than a change to
    ``ProductSerializer``: ``options`` stays a writable nested field there, and
    the dashboard ``ProductViewSet`` (``/api/dashboard/products/``) uses that
    serializer for create/update. Same JSON either way.
    """

    options = serializers.SerializerMethodField()

    @extend_schema_field(OptionSerializer(many=True))
    def get_options(self, obj) -> list[dict[str, Any]]:
        combined = list(obj.product_options.all())
        product_class = obj.get_product_class()
        if product_class is not None:
            combined += list(product_class.options.all())
        # The property's queryset union de-duplicates and orders by
        # (order, name); reproduce both in Python over the prefetched lists.
        unique = {option.pk: option for option in combined}
        ordered = sorted(unique.values(), key=lambda o: (o.order, o.name or ""))
        return OptionSerializer(ordered, many=True, context=self.context).data

    class Meta(ProductSerializer.Meta):
        pass


class RecommendedProductSerializer(serializers.ModelSerializer):
    """Compact product card for the "recommended" section of product detail.

    Deliberately not ``ProductSerializer``: that one nests services (whose
    ``available_time_slots`` walks opening hours, closures and holds for every
    day up to ``max_notice_days``), child variants and a per-branch stock
    record lookup. Repeating all of that ten times inside a single detail
    response is far too expensive; a card only needs a picture and a price.
    """

    url = serializers.HyperlinkedIdentityField(view_name="product-detail")
    images = ProductImageSerializer(many=True, read_only=True)

    class Meta:
        model = Product
        fields = (
            "id",
            "url",
            "title",
            "structure",
            "images",
            "price_currency",
            "original_price",
            "selling_price",
            "calories",
            "preparation_time",
        )


class ProductDetailSerializer(ProductSerializer):
    """``ProductSerializer`` plus the storefront's "recommended" section.

    Only the detail endpoint uses this. ``ProductSerializer`` itself is also
    the list serializer (and is nested per product by ``CategorySerializer``),
    so putting the section there would run the recommendation query once per
    row of every listing.
    """

    recommended = serializers.SerializerMethodField()

    @extend_schema_field(RecommendedProductSerializer(many=True))
    def get_recommended(self, obj) -> list[dict[str, Any]]:
        """Up to ten random products from the same category, at the same branch.

        Not to be confused with the ``recommended_products`` m2m, which is
        Oscar's hand-picked cross-sell list. Nothing populates that, so it is
        no longer serialized here (see PRODUCTDETAIL_FIELDS); the basket
        recommendation engine still reads the column.
        """
        # Lazy import: server.apps.catalogue imports back into this module.
        from server.apps.catalogue.recommendations import get_product_recommendations

        request = self.context.get("request")
        products = get_product_recommendations(
            obj, branch=resolve_branch_id(request)
        )
        return RecommendedProductSerializer(
            products, many=True, context=self.context
        ).data

    class Meta(PublicProductSerializer.Meta):
        fields = tuple(settings.PRODUCTDETAIL_FIELDS) + ("recommended",)


class ProductLinkSerializer(ProductSerializer):
    """
    Summary serializer for list view, listing all products.

    This serializer can be easily made to show any field on ``ProductSerializer``,
    just add fields to the ``OSCARAPI_PRODUCT_FIELDS`` setting.
    """

    class Meta(PublicProductSerializer.Meta):
        fields = settings.PRODUCT_FIELDS


class OptionValueSerializer(serializers.Serializer):  # pylint: disable=abstract-method
    """
    Serializer for option values when adding products to basket.
    Now accepts AttributeOption ID instead of option name.
    """
    option = serializers.HyperlinkedRelatedField(
        view_name="option-detail", 
        queryset=Option.objects
    )
    # Changed from CharField to PrimaryKeyRelatedField to accept AttributeOption ID
    value = serializers.PrimaryKeyRelatedField(
        queryset=AttributeOption.objects,
        help_text="The ID of the selected AttributeOption"
    )
    
    def to_representation(self, instance):
        """
        Return both ID and name in the response for convenience
        """
        ret = super().to_representation(instance)
        # Optionally include the option name in responses
        if 'value' in ret and ret['value'] is not None:
            try:
                option = AttributeOption.objects.get(id=ret['value'])
                ret['value_name'] = option.option
                ret['value_price'] = str(option.price) if option.price else None
            except AttributeOption.DoesNotExist:
                pass
        return ret


class AddProductSerializer(serializers.Serializer):  # pylint: disable=abstract-method
    """
    Serializes and validates an add to basket request.
    """

    quantity = serializers.IntegerField(required=True)
    branch_id = serializers.IntegerField(required=True)
    confirm = serializers.BooleanField(required=False)
    note = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    service_id = serializers.IntegerField(required=False, allow_null=True)
    service_start_at = serializers.DateTimeField(required=False, allow_null=True)
    url = serializers.HyperlinkedRelatedField(
        view_name="product-detail", queryset=Product.objects, required=True
    )
    options = OptionValueSerializer(many=True, required=False)

    def validate(self, attrs):
        """
        Ensure all required product options are provided with valid AttributeOption IDs.
        Also validate the service slot when booking a service-type product.
        """
        product = attrs.get("url")
        provided_options = attrs.get("options", []) or []

        service_id = attrs.get("service_id")
        service_start_at = attrs.get("service_start_at")

        has_services = product.service.exists()
        if has_services and not (service_id and service_start_at):
            raise serializers.ValidationError(
                {"service_id": _(
                    "This product requires a service time slot. "
                    "Please provide both service_id and service_start_at."
                )}
            )

        if service_id or service_start_at:
            if not (service_id and service_start_at):
                raise serializers.ValidationError(
                    {"service_start_at": _("Both service_id and service_start_at are required to book a slot.")}
                )
            # One basket, one booking: a slot is always quantity 1, and the
            # basket holds a single booking, so extra capacity means a
            # separate order.
            if attrs.get("quantity") != 1:
                raise serializers.ValidationError(
                    {"quantity": _(
                        "A service booking must have quantity 1. "
                        "Place a separate order for an additional service."
                    )}
                )
            try:
                service_obj = product.service.get(id=service_id)
            except Service.DoesNotExist:
                raise serializers.ValidationError(
                    {"service_id": _("Selected service is not available for this product.")}
                )
            try:
                service_obj.validate_booking(service_start_at)
            except ValueError as exc:
                raise serializers.ValidationError({"service_start_at": str(exc)})

            booked_count = service_obj.get_booked_count_for_slot(service_start_at)
            if booked_count >= service_obj.max_services_per_slot:
                raise serializers.ValidationError(
                    {"service_start_at": _(
                        "This time slot is fully booked. "
                        "Please select a different time."
                    )}
                )
            attrs["_resolved_service"] = service_obj

        # Build a map of provided option id -> AttributeOption object
        option_id_to_value = {}
        for item in provided_options:
            option = item.get("option")
            value = item.get("value")  # Now this is an AttributeOption object
            if option is not None:
                option_id_to_value[getattr(option, "id", option)] = value

        # Determine required options for the product
        required_options = [opt for opt in product.options.all() if getattr(opt, "required", False)]

        missing_names = []
        for opt in required_options:
            val = option_id_to_value.get(opt.id, None)
            # Value is now an AttributeOption object, so check if it's None
            if val is None:
                missing_names.append(opt.name)

        if missing_names:
            raise serializers.ValidationError(
                {
                    "options": _(
                        "Missing required options: %(opts)s"
                    )
                    % {"opts": ", ".join(missing_names)}
                }
            )

        # Validate that provided AttributeOption IDs belong to the correct option group
        invalid_messages = []
        for item in provided_options:
            option = item.get("option")
            value = item.get("value")  # AttributeOption object

            # Skip if no value provided
            if value is None:
                continue

            # Verify the AttributeOption belongs to this Option's option_group
            if hasattr(option, "option_group") and option.option_group:
                allowed_option_ids = set(
                    option.option_group.options.values_list("id", flat=True)
                )
                
                # Get the value ID (it's already an AttributeOption object)
                value_id = value.id if hasattr(value, 'id') else value
                
                if value_id not in allowed_option_ids:
                    invalid_messages.append(
                        _("Invalid option value ID %(val)s for option '%(name)s'")
                        % {"val": value_id, "name": option.name}
                    )

        if invalid_messages:
            raise serializers.ValidationError({"options": " ".join(invalid_messages)})

        return attrs
