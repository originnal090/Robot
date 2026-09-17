using System;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;

public static class FireEffectsValidation
{
    private const string ScenePath = "Assets/TirgamesAssets/Factory/Scenes/FactoryDay.unity";
    private const string FirePrefabPath = "Assets/FireEffects/EditableFireVisual.prefab";

    [MenuItem("Tools/Fire Effects/Validate Configuration")]
    public static void ValidateConfiguration()
    {
        var previousScene = EditorSceneManager.GetActiveScene();
        var scene = EditorSceneManager.OpenScene(ScenePath, OpenSceneMode.Single);

        try
        {
            var spray = UnityEngine.Object.FindFirstObjectByType<ExtinguisherSprayController>();
            var lightControl = UnityEngine.Object.FindFirstObjectByType<LightControl>();

            Require(spray != null, "场景中缺少 ExtinguisherSprayController。");
            Require(lightControl != null, "场景中缺少 LightControl。");
            Require(Mathf.Approximately(spray.sprayRange, 6f),
                $"喷射射程必须为 6 米，当前为 {spray.sprayRange} 米。");
            Require(Mathf.Approximately(spray.sprayConeAngle, 18f),
                $"喷射判定半角必须为 18 度，当前为 {spray.sprayConeAngle} 度。");
            Require(lightControl.firePrefab != null, "LightControl 没有引用火焰预制体。");
            Require(AssetDatabase.GetAssetPath(lightControl.firePrefab) == FirePrefabPath,
                $"必须引用可编辑火焰预制体 {FirePrefabPath}，当前为 {AssetDatabase.GetAssetPath(lightControl.firePrefab)}。");
            Require(Mathf.Approximately(lightControl.fireScaleMultiplier, 1f),
                $"原始火焰尺寸倍率必须为 1，当前为 {lightControl.fireScaleMultiplier}。");
            Require(lightControl.fireVisualScale == new Vector3(2f, 1.3f, 2f),
                $"火焰视觉缩放必须为 (2, 1.3, 2)，当前为 {lightControl.fireVisualScale}。");
            Require(Mathf.Approximately(lightControl.fireEmissionMultiplier, 2f),
                $"火焰粒子发射倍率必须为 2，当前为 {lightControl.fireEmissionMultiplier}。");
            Require(Mathf.Approximately(lightControl.fireLightIntensityMultiplier, 1.5f),
                $"火光强度倍率必须为 1.5，当前为 {lightControl.fireLightIntensityMultiplier}。");

            Debug.Log("[FireEffectsValidation] PASS");
        }
        finally
        {
            if (previousScene.IsValid() && previousScene.path != scene.path)
                EditorSceneManager.OpenScene(previousScene.path, OpenSceneMode.Single);
        }
    }

    private static void Require(bool condition, string message)
    {
        if (!condition)
            throw new InvalidOperationException(message);
    }
}
