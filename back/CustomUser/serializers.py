from rest_framework import serializers

from .models import CustomUser


class UserSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True)
    phone_number = serializers.CharField(required=True)

    class Meta:
        model = CustomUser
        fields = [
            "username",
            "email",
            "firstname",
            "lastname",
            "phone_number",
            "password",
        ]

    def create(self, validated_data):
        user = CustomUser(
            username=validated_data["username"],
            email=validated_data["email"],
            firstname=validated_data["firstname"],
            lastname=validated_data["lastname"],
            phone_number=validated_data["phone_number"],
        )
        user.set_password(validated_data["password"])
        user.save()
        return user


class AdminUserSerializer(serializers.ModelSerializer):
    class Meta:
        model = CustomUser
        fields = [
            "username",
            "email",
            "phone_number",
            "payement_status",
            "is_staff",
        ]


class AdminModifyUserSerializer(serializers.ModelSerializer):
    class Meta:
        model = CustomUser
        fields = [
            "username",
            "email",
            "firstname",
            "lastname",
            "phone_number",
            "payement_status",
            "is_staff",
            "longitude",
            "latitude",
        ]


class AdminCreateUserSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True)
    phone_number = serializers.CharField(required=True)

    class Meta:
        model = CustomUser
        fields = [
            "username",
            "email",
            "firstname",
            "lastname",
            "phone_number",
            "password",
            "is_staff",
        ]

    def create(self, validated_data):
        user = CustomUser(
            username=validated_data["username"],
            email=validated_data["email"],
            firstname=validated_data["firstname"],
            lastname=validated_data["lastname"],
            phone_number=validated_data["phone_number"],
            is_staff=validated_data["is_staff"],
        )
        user.set_password(validated_data["password"])
        user.save()
        return user
