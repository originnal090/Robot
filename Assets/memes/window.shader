Shader "Custom/StainedGlass_Emission_UV2"
{
    Properties
    {
        _MainTex ("Stained Glass Texture", 2D) = "white" {}

        _BaseBrightness ("Base Brightness", Range(0, 2)) = 1

        _EmissionStrength ("Emission Strength", Range(0, 20)) = 4

        _EmissionPower ("Emission Contrast", Range(0.2, 4)) = 1.5
    }

    SubShader
    {
        Tags
        {
            "RenderType" = "Opaque"
            "Queue" = "Geometry"
        }

        CGPROGRAM

        #pragma surface surf Lambert fullforwardshadows

        sampler2D _MainTex;

        float _BaseBrightness;
        float _EmissionStrength;
        float _EmissionPower;

        struct Input
        {
            float2 uv2_MainTex;
        };

        void surf(Input IN, inout SurfaceOutput o)
        {
            fixed4 tex = tex2D(_MainTex, IN.uv2_MainTex);

            o.Albedo =
                tex.rgb *
                _BaseBrightness;

            o.Alpha = 1.0;

            float luminance =
                dot(
                    tex.rgb,
                    float3(0.2126, 0.7152, 0.0722)
                );

            float emissionMask =
                pow(
                    saturate(luminance),
                    _EmissionPower
                );

            o.Emission =
                tex.rgb *
                emissionMask *
                _EmissionStrength;
        }

        ENDCG
    }

    FallBack "Diffuse"
}