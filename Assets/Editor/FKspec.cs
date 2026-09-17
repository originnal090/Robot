using UnityEngine;
using UnityEditor;
 
public static class ConvertGltfMaterialToStandard
{
    [MenuItem("Tools/Materials/Convert Selected glTF to Standard (No Specular)")]
    private static void ConvertSelected()
    {
        Object[] selectedObjects = Selection.objects;

        int convertedCount = 0;

        foreach (Object obj in selectedObjects)
        {
            Material mat = obj as Material;
            if (mat == null)
                continue;

            ConvertMaterial(mat);
            convertedCount++;
        }

        AssetDatabase.SaveAssets();
        AssetDatabase.Refresh();

        Debug.Log($"Converted {convertedCount} material(s) to Standard with Specular Highlights disabled.");
    }

    private static void ConvertMaterial(Material mat)
    {
        Undo.RecordObject(mat, "Convert glTF Material to Standard");

        // ------------------------------------------------------------
        // 1. 在切换 Shader 前，把 glTF 材质数据先保存下来
        // ------------------------------------------------------------

        Color baseColor = GetColor(
            mat,
            Color.white,
            "_BaseColor",
            "_Color"
        );

        string baseTextureProperty = FindTextureProperty(
            mat,
            "_BaseColorTex",
            "_BaseColorTexture",
            "_BaseColorMap",
            "_MainTex"
        );

        Texture baseTexture = null;
        Vector2 textureScale = Vector2.one;
        Vector2 textureOffset = Vector2.zero;

        if (baseTextureProperty != null)
        {
            baseTexture = mat.GetTexture(baseTextureProperty);
            textureScale = mat.GetTextureScale(baseTextureProperty);
            textureOffset = mat.GetTextureOffset(baseTextureProperty);
        }

        float metallic = GetFloat(
            mat,
            0f,
            "_Metallic",
            "_MetallicFactor"
        );

        float roughness = GetFloat(
            mat,
            1f,
            "_Roughness",
            "_RoughnessFactor"
        );

        // Standard 使用 Smoothness，而 glTF 使用 Roughness
        float smoothness = 1f - Mathf.Clamp01(roughness);


        // ------------------------------------------------------------
        // 2. 切换到 Unity Built-in Standard Shader
        // ------------------------------------------------------------

        Shader standardShader = Shader.Find("Standard");

        if (standardShader == null)
        {
            Debug.LogError(
                $"Cannot find Unity Standard shader. Material: {mat.name}"
            );
            return;
        }

        mat.shader = standardShader;


        // ------------------------------------------------------------
        // 3. 写入数据
        // ------------------------------------------------------------

        mat.SetColor("_Color", baseColor);

        mat.SetTexture("_MainTex", baseTexture);
        mat.SetTextureScale("_MainTex", textureScale);
        mat.SetTextureOffset("_MainTex", textureOffset);

        mat.SetFloat("_Metallic", metallic);
        mat.SetFloat("_Glossiness", smoothness);


        // ------------------------------------------------------------
        // 4. 强制关闭高光
        // ------------------------------------------------------------

        if (mat.HasProperty("_SpecularHighlights"))
            mat.SetFloat("_SpecularHighlights", 0f);

        mat.EnableKeyword("_SPECULARHIGHLIGHTS_OFF");


        // ------------------------------------------------------------
        // 5. 明确设置为 Opaque
        // ------------------------------------------------------------

        mat.SetFloat("_Mode", 0f);

        mat.SetOverrideTag("RenderType", "");
        mat.SetInt("_SrcBlend", (int)UnityEngine.Rendering.BlendMode.One);
        mat.SetInt("_DstBlend", (int)UnityEngine.Rendering.BlendMode.Zero);
        mat.SetInt("_ZWrite", 1);

        mat.DisableKeyword("_ALPHATEST_ON");
        mat.DisableKeyword("_ALPHABLEND_ON");
        mat.DisableKeyword("_ALPHAPREMULTIPLY_ON");

        mat.renderQueue = -1;


        EditorUtility.SetDirty(mat);

        Debug.Log(
            $"Converted: {mat.name} | " +
            $"Metallic={metallic:F2}, " +
            $"Roughness={roughness:F2}, " +
            $"Smoothness={smoothness:F2}"
        );
    }


    // ================================================================
    // Property Helpers
    // ================================================================

    private static float GetFloat(
        Material mat,
        float fallback,
        params string[] propertyNames)
    {
        foreach (string property in propertyNames)
        {
            if (mat.HasProperty(property))
                return mat.GetFloat(property);
        }

        return fallback;
    }


    private static Color GetColor(
        Material mat,
        Color fallback,
        params string[] propertyNames)
    {
        foreach (string property in propertyNames)
        {
            if (mat.HasProperty(property))
                return mat.GetColor(property);
        }

        return fallback;
    }


    private static string FindTextureProperty(
        Material mat,
        params string[] propertyNames)
    {
        foreach (string property in propertyNames)
        {
            if (mat.HasProperty(property))
                return property;
        }

        return null;
    }
}