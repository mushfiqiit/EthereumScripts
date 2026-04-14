import sys
import pandas as pd
import matplotlib.pyplot as plt
 
def main():
    if len(sys.argv) < 2:
        print("Usage: python scatter_plot_weth_usdc.py <input.csv> [output.png]")
        sys.exit(1)
 
    csv_path = sys.argv[1]
    output_path = sys.argv[2] if len(sys.argv) >= 3 else "scatter_plot.png"
 
    df = pd.read_csv(csv_path)
 
    required_cols = {"block_number", "weth_to_usdc_rate"}
    if not required_cols.issubset(df.columns):
        missing = required_cols - set(df.columns)
        print(f"Error: CSV is missing required columns: {missing}")
        sys.exit(1)
 
    df["block_number"] = pd.to_numeric(df["block_number"], errors="coerce")
    df["weth_to_usdc_rate"] = pd.to_numeric(df["weth_to_usdc_rate"], errors="coerce")
    df = df.dropna(subset=["block_number", "weth_to_usdc_rate"])
 
    fig, ax = plt.subplots(figsize=(12, 6))
 
    ax.scatter(df["block_number"], df["weth_to_usdc_rate"], s=10, alpha=0.7, color="steelblue")
    ax.xaxis.set_major_formatter(plt.matplotlib.ticker.FuncFormatter(lambda x, _: f"{int(x):,}"))
    plt.xticks(rotation=45, ha="right")
 
    ax.set_xlabel("Block Number", fontsize=13)
    ax.set_ylabel("WETH to USDC Rate", fontsize=13)
    ax.set_title("WETH/USDC Rate vs Block Number", fontsize=15)
 
    center = 2372
    margin = 50
    ax.set_ylim(center - margin, center + margin)
 
    ax.grid(True, linestyle="--", alpha=0.5)
    plt.tight_layout()
    plt.savefig(output_path, dpi=150)
    print(f"Scatter plot saved to: {output_path}")
 
if __name__ == "__main__":
    main()